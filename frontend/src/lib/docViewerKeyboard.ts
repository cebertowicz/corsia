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

/** What the document's keyboard handling reads from the viewer. */
export interface DocumentKeyState {
	/** The canvas shows an image viewer over the document; the document answers no keys. */
	covered: boolean;
	commentUI: { state: { kind: string } | null; anchor: unknown; close: () => void; restoreFocus: () => void };
	draftController: { draft: unknown; cancel: () => void; pending: unknown; select: (selection: null) => void };
	paneOpen: boolean;
	isPaneDrawer: boolean;
	escapeCloses: boolean;
	flush: () => void;
	close: () => void;
}

export function handleDocumentKey(event: KeyboardEvent, state: DocumentKeyState): void {
	if (event.defaultPrevented || state.covered) return;
	const { commentUI, draftController } = state;
	if (event.key === 'Escape' && commentUI.state?.kind === 'list' && event.target === commentUI.anchor) {
		event.preventDefault();
		commentUI.close();
		commentUI.restoreFocus();
		return;
	}
	handleDocumentEscape(event, {
		hasCommentDraft: draftController.draft !== null,
		clearCommentDraft: draftController.cancel,
		paneOpen: state.paneOpen,
		isPaneDrawer: state.isPaneDrawer,
		hasPendingSelection: Boolean(draftController.pending),
		clearPendingSelection: () => draftController.select(null),
		escapeCloses: state.escapeCloses,
		flush: state.flush,
		close: state.close,
	});
}
