<script lang="ts">
	import { onMount } from 'svelte';
	import type * as Y from 'yjs';
	import { liveDocumentBinding } from '$lib/cm/live-document-binding';
	import { EditorView, keymap } from '@codemirror/view';
	import { EditorState, Compartment, Transaction } from '@codemirror/state';
	import { history, historyKeymap, defaultKeymap, indentWithTab } from '@codemirror/commands';
	import { markdown } from '@codemirror/lang-markdown';
	import { GFM } from '@lezer/markdown';
	import { docTheme, docHighlight } from '$lib/cm/theme';
	import { markdownLivePreview } from '$lib/cm/markdown-live';
	import { imageContext, imageOpener, type OpenImageRequest } from '$lib/cm/markdown-image';
	import { linkOpener } from '$lib/cm/markdown-link';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import { markdownTasks } from '$lib/cm/markdown-tasks';
	import { commentMarks, setCommentRanges, scrollToRange, type CommentRange, type ScrollOrigin } from '$lib/cm/comment-marks';
	import { pendingCommentMark, setPendingComment } from '$lib/cm/pending-comment-mark';
	import { selectionRange, type CommentSelection } from '$lib/stores/commentSelection';
	import { ANCHOR_MOVED } from '$lib/components/ui/Popover.svelte';
	import { commentRail } from '$lib/cm/comment-rail';
	import { widgetComments } from '$lib/cm/widget-comments';
	import { tableCommentLabel } from '$lib/cm/widget-cells';
	import { suggestionMarks } from '$lib/cm/suggestion-marks';
	import { presenceMarks, setPresenceRanges, type PresenceRange } from '$lib/cm/presence-marks';
	import { createTypedReveal, revealTypedUnder, settleTypedReveals } from '$lib/cm/typed-reveal';
	import { attachPreviewFeed, type PreviewFrameSource } from '$lib/stores/livePreviewFeed';
	import { captureDocumentPlace, type TextDocumentPlace } from '$lib/documentImagePlace';

	let {
		value = $bindable(''),
		liveText = null,
		typingAllowed = false,
		previews = null,
		readOnly = false,
		imageBase = null,
		onOpenImage,
		onOpenLink,
		onSave,
		onSelect,
		onAnchorFocus,
		onSuggestionHover,
		onEdit,
		onLayout,
		highlights = [],
		presence = [],
		pendingComment = null,
		scrollTo = null,
		tabInstanceId = null,
		onDocumentPlace
	}: {
		value?: string;
		liveText?: Y.Text | null;
		typingAllowed?: boolean;
		/** Where an agent's edits arrive while it is still writing them; they type out before they are saved. */
		previews?: PreviewFrameSource | null;
		readOnly?: boolean;
		/** Where this document lives, so its markdown images resolve against it; none means they stay links. */
		imageBase?: { path: string; projectId: string | null } | null;
		/** Opens a drawn image larger in the canvas; without it an image is only selected. */
		onOpenImage?: (request: OpenImageRequest) => void;
		/** Follows a link written in this document, given its target as written; without it a link is only styled. */
		onOpenLink?: (href: string) => void;
		onSave?: () => void;
		onSelect?: (sel: { from: number; to: number; text: string; top: number; left: number; anchor?: () => DOMRect | null; label?: string; above?: boolean } | null) => void;
		onAnchorFocus?: (id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean) => void;
		onSuggestionHover?: (id: string | null) => void;
		onEdit?: (mapPos: (pos: number) => number) => void;
		onLayout?: (mapPos?: (pos: number) => number) => void;
		highlights?: CommentRange[];
		presence?: PresenceRange[];
		pendingComment?: CommentSelection | null;
		scrollTo?: { from: number; to: number; focus?: boolean; origin: ScrollOrigin } | null;
		/** The mounted tab identity keeps a delayed scroll sample from landing on the next tab. */
		tabInstanceId?: string | null;
		onDocumentPlace?: (instanceId: string, place: TextDocumentPlace) => void;
	} = $props();

	// Read at each remote update, so a dropped connection stops typing without a reconfigure.
	let typingOk = false;
	const typedReveal = createTypedReveal({ canType: () => typingOk });
	let host = $state<HTMLDivElement>();
	let view = $state<EditorView>();
	const roComp = new Compartment();
	const documentComp = new Compartment();
	let activeText: Y.Text | null = null;
	let binding: ReturnType<typeof liveDocumentBinding> | undefined;
	const localHistory = [history(), keymap.of(historyKeymap)];
	let dragging = false;
	let syncing = false;
	let curHighlights: CommentRange[] = [];

	function emitSelection(v: EditorView) {
		if (!onSelect) return;
		const r = v.state.selection.main;
		if (r.empty) { onSelect(null); return; }
		const coords = v.coordsAtPos(r.to), text = v.state.sliceDoc(r.from, r.to);
		onSelect({
			from: r.from, to: r.to, text,
			top: coords?.top ?? 0, left: coords?.right ?? coords?.left ?? 0,
			anchor: composerAnchor(v, r.from, r.to),
			label: tableCommentLabel(v.state, r.from, r.to, text) ?? undefined,
		});
	}

	/**
	 * Where the new-comment card sits. Once its composer is open the draft's own captured range is the answer, because
	 * that range already follows every local and remote edit; the selection's offsets are only good until then. A range
	 * the draft has lost keeps the card where it was last drawn, so its message stays readable.
	 */
	function composerAnchor(v: EditorView, from: number, to: number): () => DOMRect | null {
		let drawn: DOMRect | null = null;
		return () => {
			const range = pendingComment ? selectionRange(pendingComment) : { from, to };
			if (!range) return drawn;
			const rows = range.to <= v.state.doc.length ? rowsOf(v, range.from, range.to) : null;
			if (rows) drawn = rows;
			return rows;
		};
	}

	let moved = false;
	/** Tell open cards their text may have moved. The layout is read once CodeMirror has finished updating. */
	function announceMoved() {
		if (moved) return;
		moved = true;
		requestAnimationFrame(() => { moved = false; window.dispatchEvent(new Event(ANCHOR_MOVED)); });
	}

	/**
	 * Where a comment's text is drawn now. The card reads it again whenever the canvas scrolls or the text moves, so it
	 * stays with its anchor. Null while the text is not drawn: the card hides until it is, and closes if the comment is gone.
	 */
	function liveAnchor(id: string): () => DOMRect | null {
		return () => {
			const range = curHighlights.find((h) => h.id === id);
			return range && view ? rowsOf(view, range.from, range.to, range.suggestion ? id : null) : null;
		};
	}

	/** Where the text column ends: the rightmost text drawn on a line the canvas shows. The editor itself runs wider, so its own edge would leave no margin for a card. */
	function textRight(v: EditorView): number {
		const area = v.scrollDOM.getBoundingClientRect();
		const span = document.createRange();
		let right = 0;
		for (const line of v.contentDOM.querySelectorAll('.cm-line')) {
			span.selectNodeContents(line);
			const drawn = span.getBoundingClientRect();
			if (drawn.width > 0 && drawn.bottom > area.top && drawn.top < area.bottom) right = Math.max(right, drawn.right);
		}
		return right;
	}

	/** The rows a range is drawn on, out to the text column's edge, so a card can sit clear of all of it. Null while they are not drawn. */
	function rowsOf(v: EditorView, from: number, to: number, suggestionId: string | null = null): DOMRect | null {
		if (from < v.viewport.from || to > v.viewport.to) return null;
		const start = v.coordsAtPos(from, 1), end = v.coordsAtPos(to, 1);
		if (!start || !end) return null;
		const content = v.contentDOM.getBoundingClientRect();
		// A suggestion's drawn insert follows its struck text and may wrap onto the next row.
		const insert = suggestionId ? v.contentDOM.querySelector(`.cm-suggestion-insert[data-suggestion-id="${suggestionId}"]`)?.getBoundingClientRect() : null;
		const top = Math.min(start.top, end.top, insert?.top ?? Infinity);
		const right = Math.min(content.right, Math.max(textRight(v), start.right, end.right, insert?.right ?? 0));
		return new DOMRect(content.left, top, Math.max(0, right - content.left), Math.max(start.bottom, end.bottom, insert?.bottom ?? -Infinity) - top);
	}

	function handleSelection(v: EditorView) {
		const r = v.state.selection.main;
		if (r.empty) {
			onSelect?.(null);
			const hit = curHighlights.find((h) => r.from >= h.from && r.from <= h.to);
			if (hit && v.coordsAtPos(r.from)) {
				if (hit.suggestion) onAnchorFocus?.(hit.id, liveAnchor(hit.id), true);
				else onAnchorFocus?.(hit.id, liveAnchor(hit.id));
			}
		} else {
			emitSelection(v);
		}
	}

	onMount(() => {
		activeText = liveText;
		binding = activeText ? liveDocumentBinding(activeText) : undefined;
		const v = new EditorView({
			doc: activeText?.toString() ?? value,
			parent: host!,
			extensions: [
				documentComp.of(binding?.extensions ?? localHistory),
				keymap.of([
					{ key: 'Mod-s', preventDefault: true, run: () => { onSave?.(); return true; } },
					indentWithTab,
					...defaultKeymap
				]),
				markdown({ extensions: GFM }),
				docTheme,
				docHighlight,
				markdownLivePreview,
				imageContext.of(() => imageBase && { docPath: imageBase.path, projectId: imageBase.projectId, token: get(token) ?? '' }),
				...(onOpenImage ? [imageOpener.of((request) => onOpenImage?.(request))] : []),
				...(onOpenLink ? [linkOpener.of((href) => onOpenLink?.(href))] : []),
				markdownTasks,
				commentMarks,
				pendingCommentMark,
				suggestionMarks(() => ({ focus: (id) => onAnchorFocus?.(id, liveAnchor(id), true), hover: (id) => onSuggestionHover?.(id) })),
				commentRail,
				widgetComments({ onSelect: (sel) => onSelect?.(sel), onFocus: (id) => onAnchorFocus?.(id, liveAnchor(id)) }),
				presenceMarks,
				typedReveal,
				EditorView.lineWrapping,
				EditorView.contentAttributes.of({ spellcheck: 'true' }),
				roComp.of(EditorState.readOnly.of(readOnly)),
				EditorView.domEventHandlers({
					mousedown(_e, v) {
						dragging = true;
						onSelect?.(null);
						const up = () => { dragging = false; requestAnimationFrame(() => handleSelection(v)); };
						window.addEventListener('mouseup', up, { once: true });
						return false;
					}
				}),
				EditorView.updateListener.of((u) => {
					if (u.docChanged) {
						value = u.state.doc.toString();
						if (!syncing) onEdit?.((p) => u.changes.mapPos(p));
					}
					if (u.docChanged || u.geometryChanged || u.viewportChanged) announceMoved();
					if (u.docChanged || u.geometryChanged || u.viewportChanged) onLayout?.(u.docChanged ? (p) => u.changes.mapPos(p) : undefined);
					if (u.selectionSet && !dragging && !u.transactions.some((t) => t.isUserEvent('select.pointer') || t.isUserEvent('select.doc-comment'))) {
						handleSelection(u.view);
					}
				})
			]
		});
		view = v;
		onLayout?.();
		return () => { v.destroy(); binding?.destroy(); };
	});

	$effect(() => {
		const source = previews;
		if (view && source) return attachPreviewFeed(view, source);
	});

	// A scroll can fire while a tab switch replaces this editor's content. The scheduled sample retains the source
	// tab key and is discarded if that key changed, so an incoming tab's 0 never overwrites the place it left.
	$effect(() => {
		const current = view;
		const instanceId = tabInstanceId;
		if (!current || !instanceId) return;
		let frame = 0;
		const capture = () => {
			frame = 0;
			if (tabInstanceId !== instanceId) return;
			const place = captureDocumentPlace(current.dom);
			if (place) onDocumentPlace?.(instanceId, place);
		};
		const onScroll = () => { if (!frame) frame = requestAnimationFrame(capture); };
		current.scrollDOM.addEventListener('scroll', onScroll, { passive: true });
		return () => {
			current.scrollDOM.removeEventListener('scroll', onScroll);
			if (frame) cancelAnimationFrame(frame);
		};
	});

	$effect(() => {
		const text = liveText;
		if (!view || text === activeText) return;
		const previous = binding;
		binding = text ? liveDocumentBinding(text) : undefined;
		activeText = text;
		const next = text?.toString() ?? value;
		syncing = true;
		try {
			// The sync plugin captures its Y.Text at construction. Recreate plugin
			// values for a different store without dispatching its snapshot as edits.
			view.setState(view.state.update({
				effects: documentComp.reconfigure(binding?.extensions ?? localHistory),
				changes: { from: 0, to: view.state.doc.length, insert: next },
			}).state);
		} finally { syncing = false; previous?.destroy(); }
	});

	$effect(() => {
		const next = value;
		if (liveText) return;
		if (view && next !== view.state.doc.toString()) {
			syncing = true;
			try { view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } }); }
			finally { syncing = false; }
		}
	});

	$effect(() => {
		const ro = readOnly;
		view?.dispatch({ effects: roComp.reconfigure(EditorState.readOnly.of(ro)) });
	});

	$effect(() => {
		typingOk = typingAllowed;
		if (!typingAllowed && view) settleTypedReveals(view);
	});

	let lastPainted = '';
	$effect(() => {
		// A marker's card anchors to the rows of the comment's text, read again as the canvas scrolls.
		const ranges = highlights.map((h) => ({
			...h,
			onMarkerFocus: h.onMarkerFocus && ((ids: string[], opener: HTMLElement) => h.onMarkerFocus!(ids, opener, liveAnchor(ids[0]))),
		}));
		const sig = ranges.map((r) => `${r.from}:${r.to}:${r.id}:${r.focused ? 1 : 0}:${r.replyWaiting ? 1 : 0}:${JSON.stringify(r.suggestion ?? null)}`).join('|');
		if (sig === lastPainted) return;
		lastPainted = sig;
		curHighlights = ranges;
		view?.dispatch({ effects: setCommentRanges.of(ranges) });
		if (view) revealTypedUnder(view, ranges);
	});

	$effect(() => { view?.dispatch({ effects: setPendingComment.of(pendingComment) }); });

	let lastPresencePainted = '';
	$effect(() => {
		const ranges = presence.map((range) => ({
			...range,
			span: { ...range.span },
			highlightSpan: range.highlightSpan ? { ...range.highlightSpan } : null,
		}));
		const sig = ranges.map((range) => [
			range.threadId, range.name, range.color, range.span.from, range.span.to,
			range.highlightSpan?.from ?? '', range.highlightSpan?.to ?? '', range.highlightKind ?? '',
		].join(':')).join('|');
		if (sig === lastPresencePainted) return;
		lastPresencePainted = sig;
		// A pending comment anchor never sits on text an agent is still typing; an edit highlight leaves typing alone.
		if (view) revealTypedUnder(view, ranges.flatMap((range) => range.highlightKind === 'comment' && range.highlightSpan ? [range.highlightSpan] : []));
		view?.dispatch({ effects: setPresenceRanges.of(ranges) });
	});

	$effect(() => {
		const target = scrollTo;
		if (!view || !target) return;
		revealTypedUnder(view, [target]);
		scrollToRange(view, target.from, target.to, target.origin);
		if (target.focus) {
			view.dispatch({ selection: { anchor: Math.min(target.from, view.state.doc.length) }, annotations: Transaction.userEvent.of('select.doc-comment') });
			if (window.matchMedia?.('(pointer: coarse)').matches) host?.focus({ preventScroll: true });
			else view.focus();
		}
	});
</script>

<div class="md-editor" bind:this={host} tabindex="-1" role="region" aria-label="Document text"></div>

<style>
	.md-editor { height: 100%; width: 100%; text-align: left; }
	.md-editor :global(.cm-editor) { height: 100%; }
</style>
