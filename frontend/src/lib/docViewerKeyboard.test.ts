import { describe, expect, it, vi } from 'vitest';
import { closeCardPopover, handleDocumentKey, type DocumentKeyState } from '$lib/docViewerKeyboard';
import { focusTrap } from '$lib/actions/focusTrap';

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

describe('closeCardPopover: Escape with a comment popover open over the text', () => {
	// The page as at 900 px: the document sits in the overlaid Workspace panel, whose focus trap closes it on Escape.
	function page(kind: 'thread' | 'draft', cardMode: DocumentKeyState['cardMode'] = 'popover') {
		const panel = document.body.appendChild(document.createElement('div'));
		const text = panel.appendChild(Object.assign(document.createElement('div'), { contentEditable: 'true', tabIndex: 0 }));
		const card = document.body.appendChild(document.createElement('div'));
		card.dataset.popover = '';
		const reply = card.appendChild(document.createElement('textarea'));
		const closePanel = vi.fn();
		const trap = focusTrap(panel, { onEscape: closePanel, autoFocus: false });
		const { state, commentUI } = setup({ cardMode, commentUI: { state: { kind, fromText: true }, anchor: null, sheetUp: false, close: vi.fn(), restoreFocus: vi.fn(), release: vi.fn(() => false) } });
		const capture = (event: KeyboardEvent) => closeCardPopover(event, state);
		window.addEventListener('keydown', capture, true);
		const escape = (from: HTMLElement) => { from.focus(); from.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })); };
		const cleanup = () => { window.removeEventListener('keydown', capture, true); trap?.destroy?.(); panel.remove(); card.remove(); };
		return { state, commentUI, text, reply, closePanel, escape, cleanup };
	}

	it('closes a popover opened from the text and puts focus back in the text, and the panel holding the document stays', () => {
		const { commentUI, text, closePanel, escape, cleanup } = page('thread');
		escape(text);
		expect(commentUI.close).toHaveBeenCalledOnce();
		expect(commentUI.restoreFocus).toHaveBeenCalledOnce();
		expect(closePanel).not.toHaveBeenCalled();
		cleanup();
	});

	it('cancels a new comment in a popover the same way, without closing the panel', () => {
		const { state, text, closePanel, escape, cleanup } = page('draft');
		escape(text);
		expect(state.draftController.cancel).toHaveBeenCalledOnce();
		expect(closePanel).not.toHaveBeenCalled();
		cleanup();
	});

	it('leaves Escape inside the card to the card, and Escape with no popover to the layers below', () => {
		const inside = page('thread');
		inside.escape(inside.reply);
		expect(inside.commentUI.close).not.toHaveBeenCalled();
		inside.cleanup();
		for (const mode of ['margin', 'sheet', 'unknown'] as const) {
			const other = page('thread', mode === 'unknown' ? 'popover' : mode);
			if (mode === 'unknown') other.state.cardMode = undefined;
			other.escape(other.text);
			expect(other.commentUI.close, mode).not.toHaveBeenCalled();
			expect(other.closePanel).toHaveBeenCalledOnce();
			other.cleanup();
		}
	});
});
