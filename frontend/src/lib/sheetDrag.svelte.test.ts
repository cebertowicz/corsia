import { describe, expect, it, vi } from 'vitest';
import { createSheetDrag } from './sheetDrag.svelte';

function pointer(type: string, clientY: number, { pointerId = 7, button = 0 } = {}) {
	const handle = document.createElement('div');
	return { type, clientY, pointerId, button, currentTarget: handle } as unknown as PointerEvent;
}

function drag(detent: 'full' | 'half' = 'full') {
	const onExpand = vi.fn();
	const onDismiss = vi.fn();
	const sheet = createSheetDrag({ detent: () => detent, height: () => 500, onExpand, onDismiss });
	return { sheet, onExpand, onDismiss };
}

describe('createSheetDrag', () => {
	it('follows the pointer down, never up, and snaps a short drag back', () => {
		const { sheet, onDismiss } = drag();
		sheet.start(pointer('pointerdown', 100));
		expect(sheet.dragging).toBe(true);
		sheet.move(pointer('pointermove', 80));
		expect(sheet.y).toBe(0);
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

	it('expands a half sheet on a quick tap, but not on a cancelled pointer', () => {
		const { sheet, onExpand } = drag('half');
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
