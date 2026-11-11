import type { EditorView } from '@codemirror/view';
import { touchMovedBeyond } from './widget-selection-types';

/**
 * Whether the press a click ends was a swipe. A finger that comes down on a table may be starting a scroll, and a click that
 * follows it, whether or not one is sent, is not the reader asking for a caret in a cell.
 */
export class CellSwipe {
	private start: { x: number; y: number } | null = null;
	private moved = false;

	constructor(private readonly host: EditorView) {
		host.dom.addEventListener('pointerdown', this.down, true);
		host.dom.addEventListener('pointermove', this.move, true);
	}

	/** True when the press the click being handled ends moved past the touch slop. */
	swiped() { return this.moved; }

	destroy() {
		this.host.dom.removeEventListener('pointerdown', this.down, true);
		this.host.dom.removeEventListener('pointermove', this.move, true);
	}

	private readonly down = (event: PointerEvent) => {
		this.moved = false;
		this.start = event.pointerType === 'touch' ? { x: event.clientX, y: event.clientY } : null;
	};

	private readonly move = (event: PointerEvent) => {
		if (this.start && touchMovedBeyond(this.start, { x: event.clientX, y: event.clientY })) this.moved = true;
	};
}
