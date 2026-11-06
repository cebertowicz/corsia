import { describe, expect, it, vi } from 'vitest';
import { handleDocumentKey, type DocumentKeyState } from '$lib/docViewerKeyboard';

function setup(overrides: Partial<DocumentKeyState> & { sheetUp?: boolean } = {}) {
	const anchor = document.createElement('button');
	const commentUI = { state: { kind: 'list' }, anchor, sheetUp: true, close: vi.fn(), restoreFocus: vi.fn() };
	const state: DocumentKeyState = {
		covered: false,
		commentUI,
		draftController: { draft: null, cancel: vi.fn(), pending: null, select: vi.fn() },
		paneOpen: false,
		isPaneDrawer: true,
		escapeCloses: true,
		flush: vi.fn(),
		close: vi.fn(),
		...overrides,
	};
	return { state, commentUI };
}

describe('handleDocumentKey Escape', () => {
	it('closes the phone comments sheet and leaves the document open when focus is outside the sheet', () => {
		const { state, commentUI } = setup();
		const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });
		document.body.dispatchEvent(event);

		handleDocumentKey(event, state);

		expect(commentUI.close).toHaveBeenCalledOnce();
		expect(commentUI.restoreFocus).toHaveBeenCalledOnce();
		expect(state.close).not.toHaveBeenCalled();
	});

	it('closes the document when no comments sheet is open', () => {
		const { state } = setup({ commentUI: { state: null, anchor: null, sheetUp: false, close: vi.fn(), restoreFocus: vi.fn() } as DocumentKeyState['commentUI'] });
		const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });
		document.body.dispatchEvent(event);

		handleDocumentKey(event, state);

		expect(state.commentUI.close).not.toHaveBeenCalled();
		expect(state.close).toHaveBeenCalledOnce();
	});
});
