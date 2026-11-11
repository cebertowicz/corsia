import type { EditorView } from '@codemirror/view';
import { TABLE } from './widget-blocks';
import { CELL_IMAGE } from './widget-open';
import { CELL_EDITOR } from './widget-cell-state';

/** The cell of `widget` whose box is nearest `at`, or null for a table with no cells. */
function nearestCell(widget: HTMLElement, at: { x: number; y: number }) {
	let best: HTMLTableCellElement | null = null, near = Infinity;
	for (const cell of widget.querySelectorAll<HTMLTableCellElement>('td, th')) {
		const box = cell.getBoundingClientRect();
		const away = Math.hypot(Math.max(box.left - at.x, 0, at.x - box.right), Math.max(box.top - at.y, 0, at.y - box.bottom));
		if (away < near) { near = away; best = cell; }
	}
	return best;
}

/**
 * The drawn cell a click or tap at `target` edits, or null when that click belongs to something else. The one answer both
 * the cell editor (which opens the cell) and the comment flows (which otherwise select a tapped cell for a comment) read, so
 * a tap that opens a cell's editor is never also taken as a selection of the cell's hidden text.
 */
export function cellClicked(host: EditorView, target: Element | null, at?: { x: number; y: number }) {
	// A click on something inside the cell that has its own job — a link or an image — belongs to that flow, not to editing.
	// The cell's own editor is excluded so a click in it stays a caret move.
	if (!target || target.closest(CELL_EDITOR) || target.closest(`a, img, ${CELL_IMAGE}`)) return null;
	const widget = target.closest<HTMLElement>(TABLE);
	// A press on the table's padding or a border is on no cell: with a point to go by, it is the nearest cell's.
	const cell = target.closest<HTMLTableCellElement>('td, th') ?? (at && widget ? nearestCell(widget, at) : null);
	// A comment drawn on words in the cell does not give the click away: it opens the comment (`widget-comments.ts`) and the
	// cell both, with the caret where the click landed.
	const row = cell?.parentElement as HTMLTableRowElement | null;
	if (!widget || !host.dom.contains(widget) || !cell || !row || host.state.readOnly) return null;
	const selection = document.getSelection();
	if (selection && !selection.isCollapsed && widget.contains(selection.anchorNode)) return null; // a drag inside the table is a comment selection, not a click to edit
	return { widget, cell, row };
}
