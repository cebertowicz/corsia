import { touchMovedBeyond } from './widget-selection-types';

/**
 * The one rule for a touch on a table: it is a tap, which opens a cell or closes the open one, unless it moves past the touch
 * slop or the browser takes it for a scroll. A finger that comes down on a table may be starting a scroll, and neither the
 * click that follows it nor the press itself is the reader asking for a caret in a cell or to leave one. Mouse and pen
 * presses are never swipes.
 */
export class CellSwipe {
	private start: { x: number; y: number } | null = null;
	private moved = false;
	private tap: { id: number; then: () => void } | null = null;

	constructor() {
		document.addEventListener('pointerdown', this.down, true);
		document.addEventListener('pointermove', this.move, true);
		document.addEventListener('pointerup', this.up, true);
		document.addEventListener('pointercancel', this.cancel, true);
	}

	/** True when the press the click being handled ends moved past the touch slop. */
	swiped() { return this.moved; }

	/**
	 * For a press that has just begun: runs `then` only if it ends as a tap. False, with nothing deferred, for a press that is
	 * not a touch, which the caller acts on at once.
	 */
	whenTapped(press: PointerEvent, then: () => void) {
		if (press.pointerType !== 'touch') return false;
		this.tap = { id: press.pointerId, then };
		return true;
	}

	destroy() {
		document.removeEventListener('pointerdown', this.down, true);
		document.removeEventListener('pointermove', this.move, true);
		document.removeEventListener('pointerup', this.up, true);
		document.removeEventListener('pointercancel', this.cancel, true);
	}

	private readonly down = (event: PointerEvent) => {
		this.moved = false;
		this.tap = null;
		this.start = event.pointerType === 'touch' ? { x: event.clientX, y: event.clientY } : null;
	};

	private readonly move = (event: PointerEvent) => {
		if (this.start && touchMovedBeyond(this.start, { x: event.clientX, y: event.clientY })) this.moved = true;
	};

	private readonly up = (event: PointerEvent) => {
		const tap = this.tap;
		this.tap = null;
		if (tap && tap.id === event.pointerId && !this.moved) tap.then();
	};

	// The browser took the touch for a scroll or a pan: it was never a tap.
	private readonly cancel = () => { this.tap = null; };
}
