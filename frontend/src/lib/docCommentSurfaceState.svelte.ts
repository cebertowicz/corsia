import { EditorView } from '@codemirror/view';
import { Transaction } from '@codemirror/state';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import type { Anchor as PopoverAnchor } from '$lib/components/ui/Popover.svelte';
import { cancelCommentCapture } from '$lib/stores/commentDraft';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { arrivedDocComments } from '$lib/stores/docComments';

/** What a card is anchored to; the popover owns the union. */
export type CommentAnchor = NonNullable<PopoverAnchor>;

export type CommentSurfaceState = { kind: 'draft' } | { kind: 'thread'; id: string; ids?: string[] } | { kind: 'list' } | null;

/**
 * Return focus to the document's editor. CodeMirror's own focus() re-applies its selection without scrolling;
 * a bare element focus() makes WebKit put the caret at the start of the editable and scroll there once the
 * DOM selection was cleared, as it is when the comment popover's focused control is removed.
 */
export function focusDocumentEditor(root: HTMLElement | undefined) {
	const content = root?.querySelector<HTMLElement>('.cm-content');
	const view = content && EditorView.findFromDOM(content);
	if (!view) return;
	const selection = view.state.selection.main;
	if (!selection.empty) view.dispatch({ selection: { anchor: selection.head }, annotations: Transaction.userEvent.of('select.doc-comment') });
	view.focus();
}

/** The canvas width (px) at which the text keeps at least 45 characters beside the 308 px margin column. */
export const MARGIN_MIN = 660;

/** Whether a canvas this wide (px) leaves room for the margin column beside its text; below it, the popover or the sheet. */
export const hasRoomForMargin = (canvasWidth: number) => canvasWidth >= MARGIN_MIN;

/** One owner for the canvas's anchored thread/draft and whole-document list. */
export function createCommentSurfaceState(restoreEditor: () => void, draftScope?: () => string, marginUp?: () => boolean) {
	let state = $state<CommentSurfaceState>(null);
	let anchor = $state<CommentAnchor | null>(null);
	/** The anchor is the point a click opened the card at: the card sits there instead of beside the text. */
	let atClick = $state(false);
	let restoreFocus = $state<() => void>(restoreEditor);
	// Derived solely from createCommentSheet's modal lifecycle; the viewer uses it to hide its sibling selection control.
	let sheetUp = $state(false);
	function restoreReplyFocus(root: () => HTMLElement | undefined) {
		restoreFocus = () => void tick().then(() => {
			const button = root()?.querySelector<HTMLElement>('[data-doc-jump] button');
			if (button) button.focus({ preventScroll: true }); else restoreEditor();
		});
	}
	/** The suggestion under the pointer: it and its card take the focus tint without a click. */
	let hovered = $state<string | null>(null);
	/** The column was shown on load, so the shell (sidebar, Workspace panel) is left as it is until the reader closes it. */
	let quiet = $state(false);
	function close() {
		state = null;
		quiet = false;
		cancelCommentCapture(draftScope?.());
	}
	function openDraft(rect: CommentAnchor) {
		anchor = rect;
		atClick = false;
		state = { kind: 'draft' };
	}
	/** A highlight picked in the text selects its card; with the margin column up it does so as a suggestion's insert does. The highlight is how a thread is opened now that no marker stands by it: its card, once shown, stamps the replies viewed. */
	function focusComment(id: string, rect: CommentAnchor, viaSuggestion = false, clicked = false) {
		const changed = pickComment(id, rect, viaSuggestion || !!marginUp?.(), clicked);
		if (changed) arrivedDocComments.set(new Set([id]));
		return changed;
	}
	function pickComment(id: string, rect: CommentAnchor, viaSuggestion: boolean, clicked: boolean) {
		// The drawer is nonmodal: a caret landing in commented text (a click, or
		// Escape collapsing a selection) keeps it open rather than swapping it
		// for that comment's popover. Pressing a suggestion in the editor still
		// focuses its card there.
		if (state?.kind === 'list' && !viaSuggestion) return false;
		const changed = get(focusedDocComment) !== id;
		if (state?.kind === 'list') { focusedDocComment.set(id); return changed; }
		if (state?.kind === 'thread' && state.ids?.includes(id)) { state = { ...state, id }; focusedDocComment.set(id); return changed; }
		focusedDocComment.set(id);
		state = { kind: 'thread', id };
		anchor = rect;
		atClick = clicked;
		restoreFocus = restoreEditor;
		return changed;
	}
	function focusMarker(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null, clicked = false) {
		state = ids.length === 1 ? { kind: 'thread', id: ids[0] } : { kind: 'thread', id: ids[0], ids };
		focusedDocComment.set(ids[0]);
		anchor = reading ?? opener;
		atClick = clicked;
		restoreFocus = () => opener.focus();
		arrivedDocComments.set(new Set(ids));
	}
	function selectMarkerThread(id: string) {
		if (state?.kind === 'thread' && state.ids?.includes(id)) state = { ...state, id };
	}
	function focusRemainingThread(ids: string[]) {
		if (state?.kind !== 'thread' || !state.ids) return;
		const after = state.ids.indexOf(state.id) + 1;
		const id = [...state.ids.slice(after), ...state.ids.slice(0, after)].find((candidate) => ids.includes(candidate));
		if (!id) { close(); return; }
		state = { ...state, id };
		focusedDocComment.set(id);
	}
	/**
	 * With the margin column up, the card the reader picked lets go: the column stays, the cards return to their lines and the
	 * highlight eases back. A caret landing in plain text and Escape both end a pick this way. Answers whether it held one.
	 */
	function release() {
		if (!marginUp?.()) return false;
		const threadId = state?.kind === 'thread' && !state.ids ? state.id : null;
		if (threadId === null && !(state?.kind === 'list' && get(focusedDocComment) !== null)) return false;
		// Back to the column on its own: it is shown the way it is on load, so it does not pull focus off the text.
		if (threadId !== null) { state = { kind: 'list' }; quiet = true; }
		focusedDocComment.set(null);
		return true;
	}
	function openOnLoad() {
		if (state) return;
		state = { kind: 'list' };
		quiet = true;
		anchor = null;
	}
	function openList(opener: HTMLElement) {
		if (state?.kind === 'list') { close(); return; }
		quiet = false;
		state = { kind: 'list' };
		anchor = opener;
		atClick = false;
		restoreFocus = () => opener.focus();
	}
	return {
		get state() { return state; }, set state(value: CommentSurfaceState) { state = value; },
		get anchor() { return anchor; }, set anchor(value: CommentAnchor | null) { anchor = value; },
		get atClick() { return atClick; },
		get restoreFocus() { return restoreFocus; },
		get sheetUp() { return sheetUp; }, set sheetUp(value: boolean) { sheetUp = value; },
		get quiet() { return quiet; }, get hovered() { return hovered; }, hover(id: string | null) { hovered = id; },
		close, release, openOnLoad, openDraft, focusComment, focusMarker, selectMarkerThread, focusRemainingThread, openList, restoreReplyFocus,
	};
}

/** Whether the margin column fits beside the text now: the canvas is wide enough and the pane is not a drawer over it. */
export function marginFits(root: HTMLElement | undefined, paneDrawer: boolean) {
	return !paneDrawer && hasRoomForMargin(root?.getBoundingClientRect().width ?? 0);
}

/**
 * A canvas that is already wide enough for the text and the margin column shows the column on load, with the shell left alone;
 * a narrower one waits for the reader to open comments. Checked once per document, when its comments are ready. Call it while a component initialises.
 */
export function openColumnOnLoad(inputs: { artifactId: () => string | null; ready: () => boolean; root: () => HTMLElement | undefined; paneDrawer: () => boolean; hasComments: () => boolean; shown: () => boolean; open: () => void }) {
	let checked: string | null = null;
	$effect(() => {
		const id = inputs.artifactId(), root = inputs.root();
		if (!id || checked === id || !inputs.ready() || !root || inputs.paneDrawer()) return;
		checked = id;
		if (inputs.hasComments() && !inputs.shown() && marginFits(root, false)) inputs.open();
	});
}
