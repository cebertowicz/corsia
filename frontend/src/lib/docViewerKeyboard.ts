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
	commentUI: { state: { kind: string; fromText?: boolean } | null; anchor: unknown; sheetUp: boolean; close: () => void; restoreFocus: () => void; release: () => boolean };
	draftController: { draft: unknown; cancel: () => void; pending: unknown; select: (selection: null) => void };
	paneOpen: boolean;
	isPaneDrawer: boolean;
	escapeCloses: boolean;
	flush: () => void;
	close: () => void;
}

/** A key that types a character, pressed outside any text field. */
function typedKey(event: KeyboardEvent): boolean {
	const target = event.target;
	const field = target instanceof HTMLElement && (target.isContentEditable || target.matches('input, textarea, select'));
	return event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing && !field;
}

export function handleDocumentKey(event: KeyboardEvent, state: DocumentKeyState): void {
	if (event.defaultPrevented || state.covered) return;
	const { commentUI, draftController } = state;
	// A raised phone sheet is the topmost layer even when focus has left it, so Escape closes it before the document.
	if (
		event.key === 'Escape' &&
		(commentUI.sheetUp || (commentUI.state?.kind === 'list' && event.target === commentUI.anchor))
	) {
		event.preventDefault();
		commentUI.close();
		commentUI.restoreFocus();
		return;
	}
	// A sheet a tap in the text opened is a glance, not a stop: the first key typed outside its fields closes it, and the
	// caret takes focus while the key is still down, so the character lands at the tapped place.
	if (commentUI.sheetUp && commentUI.state?.kind === 'thread' && commentUI.state.fromText && typedKey(event)) {
		commentUI.close();
		commentUI.restoreFocus();
		return;
	}
	// A card picked in the margin lets go before anything else closes (a draft or a pending selection still come first).
	if (event.key === 'Escape' && !draftController.draft && !draftController.pending && commentUI.release()) {
		event.preventDefault();
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
