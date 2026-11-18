import { get } from 'svelte/store';
import { phoneNavOver } from '$lib/stores/phoneNav.svelte';

export function handleDocumentEscape(
	event: KeyboardEvent,
	state: {
		/** The composer is open; a draft kept while it is closed is not. */
		composing: boolean;
		/** Closes the composer and keeps what it holds. */
		closeComposer: () => void;
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
	if (state.composing) return state.closeComposer();
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
	draftController: { pending: unknown; select: (selection: null) => void };
	/** How a card opened from the text shows now (`DocCommentSurface.cardMode`). */
	cardMode?: 'margin' | 'popover' | 'sheet';
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
	// The phone navigation over the document covers it as an image does: keys pressed there are the navigation's.
	if (event.defaultPrevented || state.covered || get(phoneNavOver)) return;
	const { commentUI, draftController } = state;
	const composing = commentUI.state?.kind === 'draft';
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
	// A card picked in the margin lets go before anything else closes (an open composer or a pending selection still come first).
	if (event.key === 'Escape' && !composing && !draftController.pending && commentUI.release()) {
		event.preventDefault();
		return;
	}
	handleDocumentEscape(event, {
		composing,
		closeComposer: () => { commentUI.close(); commentUI.restoreFocus(); },
		paneOpen: state.paneOpen,
		isPaneDrawer: state.isPaneDrawer,
		hasPendingSelection: Boolean(draftController.pending),
		clearPendingSelection: () => draftController.select(null),
		escapeCloses: state.escapeCloses,
		flush: state.flush,
		close: state.close,
	});
}

/**
 * Escape with a comment card open over the text (the popover below the margin width) and focus outside it, such as the
 * caret in the text, closes the card and puts focus back in the text. It is read in the capture phase, before any layer
 * holding the document hears it, so an overlaid Workspace panel never closes for it. Inside the card, the card's own Escape answers.
 */
export function closeCardPopover(event: KeyboardEvent, state: DocumentKeyState): void {
	const { commentUI } = state;
	const kind = commentUI.state?.kind;
	if (event.key !== 'Escape' || event.defaultPrevented || state.covered || state.cardMode !== 'popover' || (kind !== 'thread' && kind !== 'draft')) return;
	if (event.target instanceof Element && event.target.closest('[data-popover]')) return;
	event.preventDefault();
	event.stopPropagation();
	// The composer closes as a card does, keeping what it holds; only its Cancel drops it.
	commentUI.close();
	commentUI.restoreFocus();
}
