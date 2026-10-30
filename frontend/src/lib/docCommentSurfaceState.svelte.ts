import { EditorView } from '@codemirror/view';
import { Transaction } from '@codemirror/state';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import type { Anchor as PopoverAnchor } from '$lib/components/ui/Popover.svelte';
import { cancelCommentCapture } from '$lib/stores/commentDraft';
import { focusedDocComment } from '$lib/stores/docCommentFocus';

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

/** One owner for the canvas's anchored thread/draft and whole-document list. */
export function createCommentSurfaceState(restoreEditor: () => void, draftScope?: () => string) {
	let state = $state<CommentSurfaceState>(null);
	let anchor = $state<CommentAnchor | null>(null);
	let restoreFocus = $state<() => void>(restoreEditor);
	// Derived solely from createCommentSheet's modal lifecycle; the viewer uses it to hide its sibling selection control.
	let sheetUp = $state(false);
	function restoreReplyFocus(root: () => HTMLElement | undefined) {
		restoreFocus = () => void tick().then(() => {
			const button = root()?.querySelector<HTMLElement>('[data-doc-reply-chip] button');
			if (button) button.focus({ preventScroll: true }); else restoreEditor();
		});
	}
	/** The suggestion under the pointer: it and its card take the focus tint without a click. */
	let hovered = $state<string | null>(null);
	function close() {
		state = null;
		cancelCommentCapture(draftScope?.());
	}
	function openDraft(rect: CommentAnchor) {
		anchor = rect;
		state = { kind: 'draft' };
	}
	function focusComment(id: string, rect: CommentAnchor, viaSuggestion = false) {
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
		restoreFocus = restoreEditor;
		return changed;
	}
	function focusMarker(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null) {
		state = ids.length === 1 ? { kind: 'thread', id: ids[0] } : { kind: 'thread', id: ids[0], ids };
		focusedDocComment.set(ids[0]);
		anchor = reading ?? opener;
		restoreFocus = () => opener.focus();
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
	function openList(opener: HTMLElement) {
		if (state?.kind === 'list') { close(); return; }
		state = { kind: 'list' };
		anchor = opener;
		restoreFocus = () => opener.focus();
	}
	return {
		get state() { return state; }, set state(value: CommentSurfaceState) { state = value; },
		get anchor() { return anchor; }, set anchor(value: CommentAnchor | null) { anchor = value; },
		get restoreFocus() { return restoreFocus; },
		get sheetUp() { return sheetUp; }, set sheetUp(value: boolean) { sheetUp = value; },
		get hovered() { return hovered; }, hover(id: string | null) { hovered = id; },
		close, openDraft, focusComment, focusMarker, selectMarkerThread, focusRemainingThread, openList, restoreReplyFocus,
	};
}
