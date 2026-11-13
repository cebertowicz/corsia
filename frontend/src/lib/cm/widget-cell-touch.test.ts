import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { cleanup, clicked, DOC, mountedEditor, open } from './widget-cell-rig';

/**
 * A press outside the open cell closes it, by the one rule that opens a cell: a touch is a tap unless it moves past the touch
 * slop or the browser takes it for a scroll. On a phone a swipe that scrolls the page or the table must not close the cell
 * and drop the keyboard; a mouse or pen press closes it as it comes down.
 */

const rigs: Rig[] = [];
const track = (r: Rig) => { rigs.push(r); return r; };
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

type Pointer = 'touch' | 'mouse' | 'pen';
const send = (target: Element, type: string, pointerType: Pointer, x: number, y: number) =>
	target.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, pointerType, pointerId: 7, clientX: x, clientY: y }));
const opened = () => { const r = track(open()); clicked(r); expect(mountedEditor(r), 'setup: the cell is open').not.toBeNull(); return r; };

describe('a press outside the open cell', () => {
	it('closes the cell when a touch ends as a tap, and not before it ends', () => {
		const r = opened();
		send(r.view.dom, 'pointerdown', 'touch', 100, 300);
		expect(mountedEditor(r), 'a finger that is still down may be starting a scroll').not.toBeNull();
		send(r.view.dom, 'pointerup', 'touch', 100, 300);
		expect(mountedEditor(r), 'the tap closed it').toBeNull();
		expect(r.view.state.doc.toString()).toBe(DOC);
	});

	it('closes the cell when a touch moves less than the touch slop', () => {
		const r = opened();
		send(r.view.dom, 'pointerdown', 'touch', 100, 300);
		send(r.view.dom, 'pointermove', 'touch', 102, 303);
		send(r.view.dom, 'pointerup', 'touch', 102, 303);
		expect(mountedEditor(r)).toBeNull();
	});

	it('leaves the cell open when a touch swipes past the touch slop', () => {
		const r = opened();
		send(r.view.dom, 'pointerdown', 'touch', 100, 300);
		send(r.view.dom, 'pointermove', 'touch', 100, 240);
		send(r.view.dom, 'pointerup', 'touch', 100, 240);
		expect(mountedEditor(r), 'a swipe that scrolls the page does not close the cell').not.toBeNull();
	});

	it('leaves the cell open when the browser takes the touch for a scroll', () => {
		const r = opened();
		send(r.view.dom, 'pointerdown', 'touch', 100, 300);
		send(r.view.dom, 'pointercancel', 'touch', 100, 300);
		send(r.view.dom, 'pointerup', 'touch', 100, 300);
		expect(mountedEditor(r)).not.toBeNull();
	});

	it('closes the cell again for a tap that comes after a swipe', () => {
		const r = opened();
		send(r.view.dom, 'pointerdown', 'touch', 100, 300);
		send(r.view.dom, 'pointermove', 'touch', 100, 240);
		send(r.view.dom, 'pointerup', 'touch', 100, 240);
		send(r.view.dom, 'pointerdown', 'touch', 100, 240);
		send(r.view.dom, 'pointerup', 'touch', 100, 240);
		expect(mountedEditor(r)).toBeNull();
	});

	it('closes the cell as a mouse or a pen comes down, and not as a touch does', () => {
		for (const pointer of ['mouse', 'pen'] as const) {
			const r = opened();
			send(r.view.dom, 'pointerdown', pointer, 100, 300);
			expect(mountedEditor(r), `${pointer} press`).toBeNull();
		}
	});

	it('is not a press elsewhere when it lands inside the open cell\'s editor, by touch or by mouse', () => {
		for (const pointer of ['touch', 'mouse'] as const) {
			const r = opened();
			const inside = mountedEditor(r)!;
			send(inside, 'pointerdown', pointer, 100, 300);
			send(inside, 'pointerup', pointer, 100, 300);
			expect(mountedEditor(r), `${pointer} press inside the cell`).not.toBeNull();
		}
	});
});
