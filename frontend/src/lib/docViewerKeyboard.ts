export function handleDocumentEscape(
	event: KeyboardEvent,
	state: {
		hasCommentDraft: boolean;
		clearCommentDraft: () => void;
		paneOpen: boolean;
		isPaneDrawer: boolean;
		hasPendingSelection: boolean;
		clearPendingSelection: () => void;
		escapeCloses: boolean;
		flush: () => void;
		close: () => void;
	},
): void {
	if (event.key !== 'Escape') return;
	if (state.hasCommentDraft) return state.clearCommentDraft();
	if (state.isPaneDrawer && state.paneOpen) return;
	if (state.hasPendingSelection) return state.clearPendingSelection();
	if (!state.escapeCloses) return;
	state.flush();
	state.close();
}
