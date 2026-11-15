import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSheetDrag } from './sheetDrag.svelte';
import { sheetHeld } from './sheetClearance';

function pointer(type: string, clientY: number, { pointerId = 7, button = 0 } = {}) {
	const handle = document.createElement('div');
	return { type, clientY, pointerId, button, currentTarget: handle } as unknown as PointerEvent;
}

function drag(expandable = false) {
	const onExpand = vi.fn();
	const onDismiss = vi.fn();
	const panel = document.createElement('div');
	Object.defineProperty(panel, 'clientHeight', { value: 500 });
	const sheet = createSheetDrag({ expandable: () => expandable, panel: () => panel, onExpand, onDismiss });
	return { sheet, onExpand, onDismiss, panel };
}

afterEach(() => { vi.useRealTimers(); });

describe('createSheetDrag', () => {
	it('follows the pointer down, never up, and snaps a short drag back', () => {
		const { sheet, onDismiss } = drag();
		sheet.start(pointer('pointerdown', 100));
		expect(sheet.dragging).toBe(true);
		sheet.move(pointer('pointermove', 80));
		expect(sheet.y).toBe(0);
		expect(sheet.pull).toBe(0);
		sheet.move(pointer('pointermove', 140));
		expect(sheet.y).toBe(40);
		sheet.finish(pointer('pointerup', 140));
		expect(sheet.dragging).toBe(false);
		expect(sheet.y).toBe(0);
		expect(onDismiss).not.toHaveBeenCalled();
	});

	it('dismisses past its dismiss distance', () => {
		const { sheet, onDismiss } = drag();
		sheet.start(pointer('pointerdown', 100));
		sheet.move(pointer('pointermove', 230));
		sheet.finish(pointer('pointerup', 230));
		expect(onDismiss).toHaveBeenCalledOnce();
	});

	it('grows with the pointer while it can, expands on a 40px rise, and drops the pull on release', () => {
		const { sheet, onExpand, onDismiss } = drag(true);
		sheet.start(pointer('pointerdown', 300));
		sheet.move(pointer('pointermove', 270));
		expect(sheet.pull).toBe(30);
		expect(sheet.y).toBe(0);
		sheet.move(pointer('pointermove', 240));
		expect(sheet.pull).toBe(60);
		sheet.finish(pointer('pointerup', 240));
		expect(onExpand).toHaveBeenCalledOnce();
		expect(onDismiss).not.toHaveBeenCalled();
		expect(sheet.pull).toBe(0);
	});

	it('a short pull settles back, and a pull that turns down still dismisses', () => {
		const { sheet, onExpand, onDismiss } = drag(true);
		sheet.start(pointer('pointerdown', 300));
		sheet.move(pointer('pointermove', 280));
		sheet.finish(pointer('pointerup', 280));
		expect(onExpand).not.toHaveBeenCalled();
		expect(sheet.pull).toBe(0);
		sheet.start(pointer('pointerdown', 300));
		sheet.move(pointer('pointermove', 260));
		sheet.move(pointer('pointermove', 430));
		expect(sheet.pull).toBe(0);
		expect(sheet.y).toBe(130);
		sheet.finish(pointer('pointerup', 430));
		expect(onDismiss).toHaveBeenCalledOnce();
	});

	it('expands a sheet that can grow on a quick tap, but not on a cancelled pointer', () => {
		const { sheet, onExpand } = drag(true);
		sheet.start(pointer('pointerdown', 100));
		sheet.finish(pointer('pointercancel', 100));
		expect(onExpand).not.toHaveBeenCalled();
		sheet.start(pointer('pointerdown', 100));
		sheet.finish(pointer('pointerup', 102));
		expect(onExpand).toHaveBeenCalledOnce();
	});

	it('ignores another button and another pointer', () => {
		const { sheet } = drag();
		sheet.start(pointer('pointerdown', 100, { button: 2 }));
		expect(sheet.dragging).toBe(false);
		sheet.start(pointer('pointerdown', 100));
		sheet.move(pointer('pointermove', 160, { pointerId: 9 }));
		expect(sheet.y).toBe(0);
		sheet.finish(pointer('pointerup', 160, { pointerId: 9 }));
		expect(sheet.dragging).toBe(true);
	});

	it('holds the sheet from the press until it has settled after the release, whatever the release does', () => {
		vi.useFakeTimers();
		for (const [to, outcome] of [[290, 'settle'], [240, 'expand'], [430, 'dismiss']] as const) {
			const { sheet, panel, onExpand, onDismiss } = drag(true);
			expect(sheetHeld(panel)).toBe(false);
			sheet.start(pointer('pointerdown', 300));
			expect(sheetHeld(panel)).toBe(true);
			sheet.move(pointer('pointermove', to));
			sheet.finish(pointer('pointerup', to));
			expect(onExpand).toHaveBeenCalledTimes(outcome === 'expand' ? 1 : 0);
			expect(onDismiss).toHaveBeenCalledTimes(outcome === 'dismiss' ? 1 : 0);
			// Still settling: the transitions back to rest run 220 ms.
			vi.advanceTimersByTime(220);
			expect(sheetHeld(panel)).toBe(true);
			vi.advanceTimersByTime(100);
			expect(sheetHeld(panel)).toBe(false);
		}
	});

	it('a new press while the sheet settles keeps holding it, and a reset lets go at once', () => {
		vi.useFakeTimers();
		const { sheet, panel } = drag(true);
		sheet.start(pointer('pointerdown', 300));
		sheet.finish(pointer('pointerup', 290));
		vi.advanceTimersByTime(200);
		sheet.start(pointer('pointerdown', 300));
		vi.advanceTimersByTime(400);
		expect(sheetHeld(panel)).toBe(true);
		sheet.reset();
		expect(sheetHeld(panel)).toBe(false);
	});
});
