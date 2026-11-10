import { describe, expect, it, vi } from 'vitest';
import { handleDocumentKey, type DocumentKeyState } from '$lib/docViewerKeyboard';

function setup(overrides: Partial<DocumentKeyState> = {}) {
	const anchor = document.createElement('button');
	const commentUI = { state: { kind: 'list' }, anchor, sheetUp: true, close: vi.fn(), restoreFocus: vi.fn(), release: vi.fn(() => false) };
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
	return { state, commentUI: state.commentUI };
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

	it('closes a phone thread sheet before the document when focus is outside the sheet', () => {
		const { state, commentUI } = setup({
			commentUI: {
				state: { kind: 'thread' },
				anchor: null,
				sheetUp: true,
				close: vi.fn(),
				restoreFocus: vi.fn(),
				release: vi.fn(() => false),
			},
		});
		const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });
		document.body.dispatchEvent(event);

		handleDocumentKey(event, state);

		expect(commentUI.close).toHaveBeenCalledOnce();
		expect(commentUI.restoreFocus).toHaveBeenCalledOnce();
		expect(state.close).not.toHaveBeenCalled();
	});

	it('closes the document when no comments sheet is open', () => {
		const { state } = setup({ commentUI: { state: null, anchor: null, sheetUp: false, close: vi.fn(), restoreFocus: vi.fn(), release: vi.fn(() => false) } as DocumentKeyState['commentUI'] });
		const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });
		document.body.dispatchEvent(event);

		handleDocumentKey(event, state);

		expect(state.commentUI.close).not.toHaveBeenCalled();
		expect(state.close).toHaveBeenCalledOnce();
	});

	it('closes the document when the desktop comments list is open and focus is outside its anchor', () => {
		const { state, commentUI } = setup();
		state.commentUI.sheetUp = false;
		const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });
		document.body.dispatchEvent(event);

		handleDocumentKey(event, state);

		expect(commentUI.close).not.toHaveBeenCalled();
		expect(state.close).toHaveBeenCalledOnce();
	});
});

describe('handleDocumentKey typing with a phone sheet up', () => {
	const key = (from: HTMLElement, init: KeyboardEventInit) => {
		const event = new KeyboardEvent('keydown', { cancelable: true, bubbles: true, ...init });
		from.dispatchEvent(event);
		return event;
	};
	const sheetFrom = (fromText: boolean) => setup({ commentUI: { state: { kind: 'thread', fromText }, anchor: null, sheetUp: true,
		close: vi.fn(), restoreFocus: vi.fn(), release: vi.fn(() => false) } });

	it('closes a sheet a tap in the text opened and gives the caret the key, without taking the key', () => {
		const { state, commentUI } = sheetFrom(true);
		const event = key(document.body, { key: 'Z' });
		handleDocumentKey(event, state);
		expect(commentUI.close).toHaveBeenCalledOnce();
		expect(commentUI.restoreFocus).toHaveBeenCalledOnce();
		expect(event.defaultPrevented).toBe(false);
	});

	it('leaves the sheet up for its own fields, shortcuts, keys that type nothing, and a sheet opened another way', () => {
		const field = document.createElement('textarea');
		document.body.appendChild(field);
		for (const [fromText, from, init] of [
			[true, field, { key: 'Z' }], [true, document.body, { key: 'z', metaKey: true }], [true, document.body, { key: 'Tab' }],
			[false, document.body, { key: 'Z' }],
		] as const) {
			const { state, commentUI } = sheetFrom(fromText);
			handleDocumentKey(key(from, init), state);
			expect(commentUI.close).not.toHaveBeenCalled();
		}
		field.remove();
	});
});
