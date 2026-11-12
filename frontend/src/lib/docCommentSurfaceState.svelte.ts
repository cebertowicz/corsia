import { EditorView } from '@codemirror/view';
import { Transaction } from '@codemirror/state';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import type { Anchor as PopoverAnchor } from '$lib/components/ui/Popover.svelte';
import { cancelCommentCapture } from '$lib/stores/commentDraft';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { arrivedDocComments } from '$lib/stores/docComments';
import { paneOverlay } from '$lib/stores/artifactCanvas';
import { PANE_DRAWER_WIDTH } from '$lib/artifactSplitLayout';
import { CELL_EDITOR, cellAtPosition, closeCell, openCellAt, openCellState } from '$lib/cm/widget-cell-state';

/** What a card is anchored to; the popover owns the union. */
export type CommentAnchor = NonNullable<PopoverAnchor>;

/** `fromText`: the thread was picked in the document's text, which the person may be editing, so its card leaves the focus there. */
export type CommentSurfaceState = { kind: 'draft' } | { kind: 'thread'; id: string; ids?: string[]; fromText?: true } | { kind: 'list' } | null;

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

/**
 * Closes the table cell open in the document's editor, and takes the focus out of it, so no caret or keyboard is left under
 * a modal sheet drawn over it. Answers how to give the caret back, as `focusDocumentEditor` does for prose: the cell at the
 * document's caret, which is still where the tap left it, opens again with the focus. Null when no cell was open.
 */
export function closeDocumentCell(root: HTMLElement | undefined): (() => void) | null {
	const content = root?.querySelector<HTMLElement>('.cm-content');
	const view = content && EditorView.findFromDOM(content);
	if (!view?.state.field(openCellState, false)) return null;
	const active = document.activeElement;
	if (active instanceof HTMLElement && active.closest(CELL_EDITOR)) active.blur();
	view.dispatch({ effects: closeCell.of(null) });
	return () => {
		const cell = view.dom.isConnected ? cellAtPosition(view.state, view.state.selection.main.head) : null;
		// The cell's editor takes the focus as it opens, so a key still down lands at the tapped place.
		if (cell) view.dispatch({ effects: openCellAt.of(cell) }); else focusDocumentEditor(root);
	};
}

/** The canvas width (px) at which the text keeps at least 45 characters beside the 308 px margin column. */
export const MARGIN_MIN = 660;

/**
 * Whether a canvas this wide (px) leaves room for the margin column beside its text; below it, the popover or the sheet.
 * A Workspace panel the shell shows over the canvas (`paneOverlay`) covers its inline end (`--pane-overlay-inset`), so that part is no room.
 */
export const hasRoomForMargin = (canvasWidth: number, overlaid = get(paneOverlay)) => canvasWidth - (overlaid ? PANE_DRAWER_WIDTH : 0) >= MARGIN_MIN;

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
	/** Where closing the card gives the caret back, when that is not the document's own caret (a table cell the card closed). */
	function returnFocusTo(restore: () => void) { restoreFocus = restore; }
	/** The suggestion under the pointer: it and its card take the focus tint without a click. */
	let hovered = $state<string | null>(null);
	/** The column was shown on load, so the shell (sidebar, Workspace panel) is left as it is until the reader closes it. */
	let quiet = $state(false);
	/** The draft was started with the margin column up: the column stays while it is written, and closing the draft goes back to it. */
	let overColumn = $state(false);
	/** Closes everything, the column under a draft included: another document, the history view. */
	function closeAll() {
		state = null;
		quiet = false;
		overColumn = false;
		cancelCommentCapture(draftScope?.());
	}
	/** Closes what is open; a draft written over the margin column closes back to the column. */
	function close() {
		if (state?.kind !== 'draft' || !overColumn) { closeAll(); return; }
		overColumn = false;
		cancelCommentCapture(draftScope?.());
		state = { kind: 'list' };
	}
	function openDraft(rect: CommentAnchor) {
		if (state?.kind !== 'draft') overColumn = (state?.kind === 'list' || state?.kind === 'thread') && !!marginUp?.();
		anchor = rect;
		atClick = false;
		state = { kind: 'draft' };
	}
	/** A highlight picked in the text selects its card; with the margin column up it does so as a suggestion's insert does. The highlight is how a thread is opened now that no marker stands by it: its card, once shown, stamps the replies viewed. */
	function focusComment(id: string, rect: CommentAnchor, viaSuggestion = false, clicked = false) {
		const margin = marginUp?.();
		// A document canvas with no room for the column shows its list as a sheet: a highlight picked there opens that one thread in its place.
		const changed = pickComment(id, rect, viaSuggestion || !!margin, clicked, margin === false);
		if (changed) arrivedDocComments.set(new Set([id]));
		return changed;
	}
	function pickComment(id: string, rect: CommentAnchor, viaSuggestion: boolean, clicked: boolean, replacesList = false) {
		// state.id identifies this surface's card, which may stay open after resolution; the shared focus store
		// identifies the highlighted thread and clears when it resolves.
		// The drawer is nonmodal: a caret landing in commented text (a click, or
		// Escape collapsing a selection) keeps it open rather than swapping it
		// for that comment's popover. Pressing a suggestion in the editor still
		// focuses its card there.
		if (state?.kind === 'list' && !viaSuggestion && !replacesList) return false;
		const changed = get(focusedDocComment) !== id;
		if (state?.kind === 'list' && !replacesList) { focusedDocComment.set(id); return changed; }
		if (state?.kind === 'thread' && state.ids?.includes(id)) { state = { ...state, id }; focusedDocComment.set(id); return changed; }
		focusedDocComment.set(id);
		state = { kind: 'thread', id, fromText: true };
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
		get quiet() { return quiet; }, get draftOverColumn() { return state?.kind === 'draft' && overColumn; }, get hovered() { return hovered; }, hover(id: string | null) { hovered = id; },
		close, closeAll, release, openOnLoad, openDraft, focusComment, focusMarker, selectMarkerThread, focusRemainingThread, openList, restoreReplyFocus, returnFocusTo,
	};
}

/** How a card opened from the text shows: in the margin column beside it, as a popover over it, or as the phone sheet. */
export type CardMode = 'margin' | 'popover' | 'sheet';

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
