import { describe, expect, it, vi } from 'vitest';
import { createSheetDrag } from './sheetDrag.svelte';

function pointer(type: string, clientY: number, { pointerId = 7, button = 0 } = {}) {
	const handle = document.createElement('div');
	return { type, clientY, pointerId, button, currentTarget: handle } as unknown as PointerEvent;
}

function drag(expandable = false) {
	const onExpand = vi.fn();
	const onDismiss = vi.fn();
	const sheet = createSheetDrag({ expandable: () => expandable, height: () => 500, onExpand, onDismiss });
	return { sheet, onExpand, onDismiss };
}

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
});
