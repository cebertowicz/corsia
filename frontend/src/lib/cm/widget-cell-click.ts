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

/** How far past the text column a press still counts as beside a table: the padding the document canvas keeps around it. */
const BESIDE = 24;

/**
 * The drawn table a press beside it belongs to: level with the table's widget, across the text column and the padding around
 * it, on the background itself (the editor's own area or what holds the editor), not on a card, a control or any other thing
 * of its own. The browser would put a caret at the table's edge there, where text typed joins the table's rows.
 */
function tableBeside(host: EditorView, target: Element, at: { x: number; y: number }): HTMLElement | null {
	if (target !== host.contentDOM && target !== host.scrollDOM && target !== host.dom && !target.contains(host.dom)) return null;
	const column = host.scrollDOM.getBoundingClientRect();
	if (at.x < column.left - BESIDE || at.x > column.right + BESIDE) return null;
	for (const widget of host.contentDOM.querySelectorAll<HTMLElement>(TABLE)) {
		const box = widget.getBoundingClientRect();
		if (at.y >= box.top && at.y <= box.bottom) return widget;
	}
	return null;
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
	const inside = target.closest<HTMLElement>(TABLE), widget = inside ?? (at ? tableBeside(host, target, at) : null);
	// A press on the table's padding or a border, or beside it, is on no cell: with a point to go by, it is the nearest cell's.
	// Beside the table the cell wins only for a plain press: a press that ends a drag has selected prose.
	if (!inside && widget && document.getSelection()?.isCollapsed === false) return null;
	const cell = target.closest<HTMLTableCellElement>('td, th') ?? (at && widget ? nearestCell(widget, at) : null);
	// A comment drawn on words in the cell does not give the click away: it opens the comment (`widget-comments.ts`) and the
	// cell both, with the caret where the click landed.
	const row = cell?.parentElement as HTMLTableRowElement | null;
	if (!widget || !host.dom.contains(widget) || !cell || !row || host.state.readOnly) return null;
	const selection = document.getSelection();
	if (selection && !selection.isCollapsed && widget.contains(selection.anchorNode)) return null; // a drag inside the table is a comment selection, not a click to edit
	return { widget, cell, row };
}
