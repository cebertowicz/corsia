import type { EditorView } from '@codemirror/view';
import { blockRange } from './widget-blocks';
import { cellClicked } from './widget-cell-click';
import { cellToOpen, openCellAt } from './widget-cell-state';
import { CellSwipe } from './widget-cell-swipe';

/** What the cell editor lends the presses: whether a cell is open, whether a node is part of its editor, and what a press does to it. */
export interface PressedCell {
	isOpen(): boolean;
	inEditor(node: Node): boolean;
	/** A cell has been asked to open by a press at this point, which is where its caret goes. */
	opened(at: { x: number; y: number }): void;
	close(): void;
}

/**
 * The presses that open and close a cell: a click on a drawn cell, or beside the table, opens it, and a press anywhere else
 * closes the open one. One rule for touch (`widget-cell-swipe.ts`): a touch counts only when it ends as a tap.
 */
export class CellPresses {
	private readonly swipe = new CellSwipe();

	constructor(private readonly host: EditorView, private readonly cell: PressedCell) {
		host.dom.addEventListener('click', this.openPressed);
		document.addEventListener('click', this.openPressedOutside);
		document.addEventListener('pointerdown', this.pressedElsewhere, true);
	}

	/** A click on a drawn cell edits it: a caret in that cell, the way a click in prose places one. */
	private readonly openPressed = (event: MouseEvent) => {
		const host = this.host;
		if (this.swipe.swiped()) return;
		const clicked = cellClicked(host, event.target instanceof Element ? event.target : null, { x: event.clientX, y: event.clientY });
		if (!clicked) return;
		const { widget, cell, row } = clicked;
		const block = blockRange(host, widget);
		if (!block) return;
		const open = cellToOpen(host.state, block.from, row.rowIndex, cell.cellIndex);
		if (open) {
			this.cell.opened({ x: event.clientX, y: event.clientY });
			host.dispatch({ effects: openCellAt.of(open) });
		}
	};

	/** A click on the canvas around the editor, level with a table: beside it, which is the nearest cell's as it is inside the editor. */
	private readonly openPressedOutside = (event: MouseEvent) => {
		if (!(event.target instanceof Node && this.host.dom.contains(event.target))) this.openPressed(event);
	};

	/** A press outside the open cell closes it. A touch closes it only if it ends as a tap, the rule that opens a cell: a swipe is a scroll. */
	private readonly pressedElsewhere = (press: PointerEvent) => {
		if (!this.cell.isOpen() || this.cell.inEditor(press.target as Node)) return;
		if (!this.swipe.whenTapped(press, () => { if (this.cell.isOpen()) this.cell.close(); })) this.cell.close();
	};

	destroy() {
		this.host.dom.removeEventListener('click', this.openPressed);
		document.removeEventListener('click', this.openPressedOutside);
		document.removeEventListener('pointerdown', this.pressedElsewhere, true);
		this.swipe.destroy();
	}
}
