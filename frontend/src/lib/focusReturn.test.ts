import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { createCloseInput, focusWithoutMoving, isTouchClose, returnFocus } from './focusReturn';

afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = ''; });

describe('returning focus', () => {
	it('restores all ancestor scroll offsets if focus ignores preventScroll', () => {
		const outer = document.createElement('div');
		const inner = document.createElement('div');
		const target = document.createElement('button');
		outer.append(inner); inner.append(target); document.body.append(outer);
		outer.scrollTop = 100; outer.scrollLeft = 9; inner.scrollTop = 70; inner.scrollLeft = 4;
		vi.spyOn(target, 'focus').mockImplementation(() => { outer.scrollTop = 0; outer.scrollLeft = 0; inner.scrollTop = 0; inner.scrollLeft = 0; });
		focusWithoutMoving(target, () => target.focus({ preventScroll: true }));
		expect([outer.scrollTop, outer.scrollLeft, inner.scrollTop, inner.scrollLeft]).toEqual([100, 9, 70, 4]);
	});

	it('puts the editor selection in the DOM before focusing', () => {
		const view = new EditorView({ parent: document.body, state: EditorState.create({ doc: 'abcdef', selection: { anchor: 2, head: 4 } }) });
		const focus = vi.spyOn(view, 'focus').mockImplementation(() => {
			expect(document.getSelection()?.toString()).toBe('cd');
		});
		returnFocus(view.contentDOM);
		expect(focus).toHaveBeenCalledOnce();
		view.destroy();
	});

	it('uses the event that closes the surface, not the device capability', () => {
		expect(isTouchClose({ type: 'pointerdown', pointerType: 'touch' })).toBe(true);
		expect(isTouchClose({ type: 'pointerdown', pointerType: 'pen' })).toBe(true);
		expect(isTouchClose({ type: 'pointerdown', pointerType: 'mouse' })).toBe(false);
		expect(isTouchClose({ type: 'keydown' })).toBe(false);
		expect(isTouchClose({ type: 'click', detail: 0 })).toBe(false);
	});

	it('consumes only the pointer for this surface and activation', () => {
		const first = createCloseInput(), second = createCloseInput();
		const button = document.createElement('button');
		const down = new PointerEvent('pointerdown', { pointerType: 'touch' });
		Object.defineProperty(down, 'target', { value: button });
		first.pointerDown(down);
		const click = new MouseEvent('click', { detail: 1 });
		Object.defineProperty(click, 'target', { value: button });
		expect(second.close(click)).toBe(false);
		expect(first.close(click)).toBe(true);
		expect(first.close(click)).toBe(false);
		first.pointerDown(down);
		const keyboardClick = new MouseEvent('click', { detail: 0 });
		Object.defineProperty(keyboardClick, 'target', { value: button });
		expect(first.close(keyboardClick)).toBe(false);
		expect(first.close(new PointerEvent('pointerup', { pointerType: 'touch' }))).toBe(true);
		first.pointerDown(down);
		first.clear();
		expect(first.close(click)).toBe(false);
	});
});
