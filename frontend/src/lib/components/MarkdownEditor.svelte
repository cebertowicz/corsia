<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import type * as Y from 'yjs';
	import { liveDocumentBinding } from '$lib/cm/live-document-binding';
	import { EditorView, keymap } from '@codemirror/view';
	import { EditorState, Compartment, type ChangeDesc } from '@codemirror/state';
	import { history, historyKeymap, defaultKeymap, indentWithTab } from '@codemirror/commands';
	import { markdown } from '@codemirror/lang-markdown';
	import { GFM } from '@lezer/markdown';
	import { docEditorLook } from '$lib/cm/theme';
	import { markdownLivePreview } from '$lib/cm/markdown-live';
	import { imageContext, imageOpener, refreshMarkdownImages, type OpenImageRequest } from '$lib/cm/markdown-image';
	import { linkOpener } from '$lib/cm/markdown-link';
	import { token } from '$lib/stores/auth';
	import { focusedDocComment } from '$lib/stores/docCommentFocus';
	import { get } from 'svelte/store';
	import { markdownTasks } from '$lib/cm/markdown-tasks';
	import { CaretRule, releasesPress } from '$lib/cm/caret-rule';
	import { commentMarks, coveredByMove, highlightAtPress, highlightsAtPress, pickHighlight, openCommentIds, paintSignature, setCommentRanges, type CommentRange } from '$lib/cm/comment-marks';
	import { pendingCommentMark, setPendingComment } from '$lib/cm/pending-comment-mark';
	import { selectionRange, type CommentSelection } from '$lib/stores/commentSelection';
	import type { CardMode } from '$lib/docCommentSurfaceState.svelte';
	import { ANCHOR_MOVED } from '$lib/components/ui/Popover.svelte';
	import { commentRail } from '$lib/cm/comment-rail';
	import { imagePins } from '$lib/cm/image-pins';
	import { commentNewSigns } from '$lib/cm/comment-new-signs';
	import { widgetComments } from '$lib/cm/widget-comments';
	import { selectionOffer, type SelectionOffer } from '$lib/cm/selection-offer';
	import { cardQuoteLabel } from '$lib/cm/widget-cells';
	import { suggestionMarks } from '$lib/cm/suggestion-marks';
	import { presenceMarks, setPresenceRanges, type PresenceRange } from '$lib/cm/presence-marks';
	import { createTypedReveal, revealTypedUnder, settleTypedReveals } from '$lib/cm/typed-reveal';
	import { jumpToComment, type JumpTarget } from '$lib/cm/comment-jump';
	import { attachPreviewFeed, type PreviewFrameSource } from '$lib/stores/livePreviewFeed';
	import { captureDocumentPlace, dispatchKeepingPlace, holdPlace, keepPlaceWhenEditorMoves, reportDocumentPlaceOnScroll, type TextDocumentPlace } from '$lib/documentReadingPlace';

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
		onCaretAway,
		cardMode = 'margin',
		onTyping,
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
		imageBase?: { path: string; projectId: string | null; imageCacheKey?: string } | null;
		/** Opens a drawn image larger in the canvas; without it an image is only selected. */
		onOpenImage?: (request: OpenImageRequest) => void;
		/** Follows a link written in this document, given its target as written; without it a link is only styled. */
		onOpenLink?: (href: string) => void;
		onSave?: () => void;
		onSelect?: (sel: SelectionOffer | null) => void;
		/** `click` is the press that opened it, when a pointer did; the card then opens at that point. */
		onAnchorFocus?: (id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean, click?: MouseEvent) => void;
		/** A caret came to rest in text no comment covers: whatever card was picked lets go. */
		onCaretAway?: () => void;
		/** How a card opened from the text shows now. Over the text (a popover or the phone sheet) only a click on a highlight opens one, never the caret arriving by keys. */
		cardMode?: CardMode;
		/** The person typed or deleted in the text. */
		onTyping?: () => void;
		onSuggestionHover?: (id: string | null) => void;
		/** `open`: the comments whose text was just typed over, before this edit (`openCommentIds`). */
		onEdit?: (changes: ChangeDesc, open: ReadonlySet<string>) => void;
		onLayout?: (mapPos?: (pos: number) => number) => void;
		highlights?: CommentRange[];
		presence?: PresenceRange[];
		pendingComment?: CommentSelection | null;
		scrollTo?: JumpTarget | null;
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
	let syncing = false;
	let curHighlights: CommentRange[] = [];

	/**
	 * Where the new-comment card sits. Once its composer is open the draft's own captured range is the answer, because
	 * that range already follows every local and remote edit; the selection's offsets are only good until then. A range
	 * the draft has lost keeps the card where it was last drawn, so its message stays readable. Text in a table cell (selected in
	 * its open editor) anchors as prose does, to the lines its words are drawn on.
	 */
	function composerAnchor(v: EditorView, from: number, to: number): () => DOMRect | null {
		let drawn: DOMRect | null = null;
		return () => {
			const range = pendingComment ? selectionRange(pendingComment) : { from, to };
			if (!range) return drawn;
			const rows = range.to <= v.state.doc.length ? cellTextRows(v, range.from, range.to) ?? rowsOf(v, range.from, range.to) : null;
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

	import { rowsOf } from '$lib/cm/comment-anchor-geometry';
	import { cellTextRows, emptyCellKey } from '$lib/cm/widget-cell-comment';
	import { openCellState } from '$lib/cm/widget-cell-state';
	import { cellEditorExtensions } from '$lib/cm/widget-cell-setup';

	/** A card's label for a comment on a table's text or its quote from code, read from this editor's own syntax tree; undefined while the editor is not up. */
	export function widgetLabel(from: number | undefined, to: number | undefined, quote: string): string | null | undefined {
		return view ? cardQuoteLabel(view.state, from, to, quote) : undefined;
	}

	/**
	 * Whether the draft's text at `from` needs revealing now the sheet that overlays this editor came up (always) or moved (only when
	 * it covered text the reader had in view); a reader's own scroll away from the draft is never undone.
	 */
	let bandBefore: { top: number; bottom: number } | null = null;
	export function draftCovered(from: number, opening: boolean): boolean {
		if (!view) return opening;
		const { band, covered } = coveredByMove(view, from, opening ? null : bandBefore);
		bandBefore = band;
		return opening || covered;
	}

	/** The document a label is read from: a new one with every edit, so a cached label knows when its table may have changed. */
	export function widgetLabelStamp(): unknown { return view?.state.doc; }

	/** This editor's view while it is up, for a caller that adds its own extension slot (an email draft's header). */
	export function editorView(): EditorView | undefined { return view; }

	/** A click on a mark drawn inside a table or diagram: one comment opens as a click on its text does, several as their margin marker does. */
	function focusWidgetComments(ids: string[], opener: HTMLElement, click?: MouseEvent, fromText = false) {
		const open = ids.length > 1 ? curHighlights.find((h) => h.id === ids[0])?.onMarkerFocus : undefined;
		if (open) open(ids, opener, undefined, click, fromText); else onAnchorFocus?.(ids[0], liveAnchor(ids[0]), opener.dataset.suggestionId !== undefined || undefined, click);
	}

	/** Which highlight the reader is in: a press decides until a key or a later press moves the caret (`caret-rule.ts`). */
	const caretRule = new CaretRule();

	function handleSelection(v: EditorView, click?: MouseEvent, typed?: boolean) {
		const r = v.state.selection.main;
		const offer = selectionOffer(v, composerAnchor);
		if (r.empty && !offer) {
			const read = caretRule.read(!!click, !!typed, () => click ? highlightAtPress(v, click, curHighlights, cardMode !== 'sheet') : pickHighlight(curHighlights.filter((h) => !h.point && r.from >= h.from && r.from <= h.to), cardMode !== 'sheet'));
			if (!read) return;
			onSelect?.(null);
			const { hit, arrived } = read;
			// Where a card would cover the text (sheet, popover), only a tap or click opens it; keys and typing are editing. A caret in a suggestion opens none on the phone; a press on its insert does (suggestionMarks).
			const opens = cardMode === 'sheet' ? !!click && !hit?.suggestion : cardMode === 'popover' ? !!click : arrived || !!click;
			const stacked = hit && click ? highlightsAtPress(v, click, curHighlights).filter((h) => !h.suggestion) : [];
			if (hit && opens && stacked.length > 1 && v.coordsAtPos(r.from)) focusWidgetComments(stacked.map((h) => h.id), (click!.target as Element | null)?.closest<HTMLElement>('.cm-comment-anchor') ?? v.dom, click, true);  // two or more threads on the pressed text open as a list, as their margin marker does
			else if (hit && opens && v.coordsAtPos(r.from)) {
				onAnchorFocus?.(hit.id, liveAnchor(hit.id), hit.suggestion ? true : undefined, click);
			} else if (!hit) onCaretAway?.();
		} else {
			caretRule.range();
			onSelect?.(offer);
		}
	}

	onMount(() => {
		activeText = liveText;
		binding = activeText ? liveDocumentBinding(activeText) : undefined;
		// The save key is the document's in prose and in an open table cell alike; a cell's edit is in the document as it is typed.
		const saveKey = keymap.of([{ key: 'Mod-s', preventDefault: true, run: () => { onSave?.(); return true; } }]);
		const v = new EditorView({
			doc: activeText?.toString() ?? value,
			parent: host!,
			dispatchTransactions: dispatchKeepingPlace,
			extensions: [
				documentComp.of(binding?.extensions ?? localHistory),
				saveKey,
				cellEditorExtensions.of(saveKey),
				keymap.of([indentWithTab, ...defaultKeymap]),
				markdown({ extensions: GFM }),
				docEditorLook,
				markdownLivePreview,
				imageContext.of(() => imageBase && { docPath: imageBase.path, projectId: imageBase.projectId, token: get(token) ?? '', imageCacheKey: imageBase.imageCacheKey }),
				...(onOpenImage ? [imageOpener.of((request) => onOpenImage?.(request))] : []),
				...(onOpenLink ? [linkOpener.of((href) => onOpenLink?.(href))] : []),
				markdownTasks,
				commentMarks,
				pendingCommentMark,
				suggestionMarks(() => ({ focus: (id, click) => onAnchorFocus?.(id, liveAnchor(id), true, click), hover: (id) => onSuggestionHover?.(id) })),
				commentRail,
				widgetComments({ onSelect: (sel) => onSelect?.(sel), onFocus: focusWidgetComments }),
				imagePins({ onFocus: focusWidgetComments }),
				commentNewSigns,
				presenceMarks,
				typedReveal,
				EditorView.lineWrapping,
				EditorView.contentAttributes.of({ spellcheck: 'true' }),
				roComp.of(EditorState.readOnly.of(readOnly)),
				EditorView.domEventHandlers({
					mousedown(press, v) {
						caretRule.pressStarted();
						onSelect?.(null);
						// The press is read a frame after mouseup, once the editor has placed the caret; a key that moves it first supersedes the press.
						const up = () => { caretRule.pressUp(press); requestAnimationFrame(() => { if (caretRule.settled(press)) handleSelection(v, press); }); };
						window.addEventListener('mouseup', up, { once: true });
						return false;
					}
				}),
				// The caret a press placed can be reported after mouseup as a plain 'select' (CodeMirror labels any report 50 ms past the pointer so, as a phone tap's can be): only a key, or a change that is not the press, drops it (`caret-rule.ts`).
				EditorView.domEventObservers({ keydown() { caretRule.released(); } }),
				EditorView.updateListener.of((u) => {
					if (u.docChanged) {
						value = u.state.doc.toString();
						if (!syncing) onEdit?.(u.changes, openCommentIds(u.startState));
						if (u.transactions.some((t) => t.isUserEvent('input') || t.isUserEvent('delete'))) onTyping?.();
					}
					if (u.docChanged || u.geometryChanged || u.viewportChanged) announceMoved();
					if (u.docChanged || u.geometryChanged || u.viewportChanged) onLayout?.(u.docChanged ? (p) => u.changes.mapPos(p) : undefined);
					if (u.transactions.some(releasesPress)) caretRule.released();
					// The editor's selection is the one writer of its Comment offer, an open empty cell's whole-cell offer included: an empty cell opening or emptied raises it, its first character takes it away.
					// The cell closing leaves the offer up, so a press on it (which closes the cell) still lands on it.
					const offerMoved = u.selectionSet || (!!u.state.field(openCellState, false) && emptyCellKey(u.startState) !== emptyCellKey(u.state));
					if (offerMoved && !caretRule.holding() && !u.transactions.some((t) => t.isUserEvent('select.pointer') || t.isUserEvent('select.doc-comment'))) {
						handleSelection(u.view, undefined, u.docChanged);
					}
				})
			]
		});
		view = v;
		const stopKeepingPlace = keepPlaceWhenEditorMoves(v);
		onLayout?.();
		return () => { stopKeepingPlace(); v.destroy(); binding?.destroy(); };
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
		return reportDocumentPlaceOnScroll(current, (place) => { if (tabInstanceId === instanceId) onDocumentPlace?.(instanceId, place); });
	});

	$effect(() => {
		const text = liveText;
		if (!view || text === activeText) return;
		const previous = binding;
		binding = text ? liveDocumentBinding(text) : undefined;
		activeText = text;
		// A different live text (a reset or replacement) cannot be trusted to still hold what was typing.
		if (previous) settleTypedReveals(view);
		const next = text?.toString() ?? value;
		syncing = true;
		try {
			// The sync plugin captures its Y.Text at construction. Recreate plugin
			// values for a different store without dispatching its snapshot as edits.
			view.setState(view.state.update({
				effects: documentComp.reconfigure(binding?.extensions ?? localHistory),
				changes: { from: 0, to: view.state.doc.length, insert: next },
			}).state);
			// Another document is open in this editor: its place is wherever it opened.
			holdPlace(view, captureDocumentPlace(view));
		} finally { syncing = false; previous?.destroy(); }
	});

	$effect(() => {
		const next = value;
		if (liveText) return;
		const current = view?.state.doc.toString();
		if (view && current !== undefined && next !== current) {
			// Only the span that differs is replaced, so a comment mark on text this change leaves alone keeps its place (a whole-document
			// replace maps every mark to the end of the document and its comment reads as 'Text changed').
			let from = 0;
			while (from < current.length && from < next.length && current[from] === next[from]) from += 1;
			let tail = 0;
			while (tail < current.length - from && tail < next.length - from && current[current.length - 1 - tail] === next[next.length - 1 - tail]) tail += 1;
			syncing = true;
			try { view.dispatch({ changes: { from, to: current.length - tail, insert: next.slice(from, next.length - tail) } }); }
			finally { syncing = false; }
		}
	});

	$effect(() => {
		const ro = readOnly;
		view?.dispatch({ effects: roComp.reconfigure(EditorState.readOnly.of(ro)) });
	});

	$effect(() => {
		imageBase?.imageCacheKey;
		view?.dispatch({ effects: refreshMarkdownImages.of() });
	});

	$effect(() => {
		// A drop or reconnect decides whether a new arrival types, not whether the ones already typing finish.
		typingOk = typingAllowed;
	});

	let lastPainted = '';
	$effect(() => {
		// A marker's card anchors to the rows of the comment's text, read again as the canvas scrolls.
		const ranges = highlights.map((h) => ({
			...h,
			onMarkerFocus: h.onMarkerFocus && ((ids: string[], opener: HTMLElement, _anchor?: () => DOMRect | null, click?: MouseEvent, fromText?: boolean) => h.onMarkerFocus!(ids, opener, () => {
				const focused = get(focusedDocComment);
				const id = focused && ids.includes(focused) ? focused : ids.find((candidate) => curHighlights.some((highlight) => highlight.id === candidate));
				return id ? liveAnchor(id)() : null;
			}, click, fromText)),
		}));
		const sig = paintSignature(ranges);
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
		const target = scrollTo, editor = view;
		if (!editor || !target) return;
		// Only a new target moves the reader. The jump reads other state (the visible band asks whether a sheet is over the
		// text); tracked, a sheet closing would replay the jump and pull focus back to the text from the sheet's opener.
		untrack(() => jumpToComment(editor, target, host));
	});
</script>

<div class="md-editor" bind:this={host} tabindex="-1" role="region" aria-label="Document text"></div>

<style>
	.md-editor { height: 100%; width: 100%; text-align: left; }
	.md-editor :global(.cm-editor) { height: 100%; }
</style>
