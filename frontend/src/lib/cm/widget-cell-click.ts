import type { EditorView } from '@codemirror/view';
import { TABLE } from './widget-blocks';
import { CELL_IMAGE } from './widget-open';
import { CELL_EDITOR } from './widget-cell-state';

/**
 * The drawn cell a click or tap at `target` edits, or null when that click belongs to something else. The one answer both
 * the cell editor (which opens the cell) and the comment flows (which otherwise select a tapped cell for a comment) read, so
 * a tap that opens a cell's editor is never also taken as a selection of the cell's hidden text.
 */
export function cellClicked(host: EditorView, target: Element | null) {
	// A click on something inside the cell that has its own job — a link, an image, a comment or suggestion drawn on the
	// cell's text — belongs to that flow, not to editing. The cell's own editor is excluded so a click in it stays a caret move.
	if (!target || target.closest(CELL_EDITOR) || target.closest(`a, img, ${CELL_IMAGE}`)) return null;
	const widget = target.closest<HTMLElement>(TABLE);
	const cell = target.closest<HTMLTableCellElement>('td, th');
	// A comment mark drawn on words inside the cell gives the click away. One on the cell itself (a whole-cell or multi-cell
	// comment) or on the table widget (a whole-table comment) is the cell's own, so the click still opens it.
	const mark = target.closest('[data-comment-ids]');
	if (mark && mark !== cell && cell?.contains(mark)) return null;
	const row = cell?.parentElement as HTMLTableRowElement | null;
	if (!widget || !host.dom.contains(widget) || !cell || !row || host.state.readOnly) return null;
	const selection = document.getSelection();
	if (selection && !selection.isCollapsed && widget.contains(selection.anchorNode)) return null; // a drag inside the table is a comment selection, not a click to edit
	return { widget, cell, row };
}
