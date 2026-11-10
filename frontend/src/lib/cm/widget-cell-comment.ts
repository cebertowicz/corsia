import type { EditorState } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { cellElement, drawnTableAt } from './widget-blocks';
import { cellSource, openCellState } from './widget-cell-state';
import { selectionIn } from './widget-selection';
import type { WidgetSelection } from './widget-selection-types';

/**
 * An empty cell has no text to select, so while its editor is open and still empty the Comment offer is the whole cell's,
 * as it is for a tap on an empty cell of a document that can't be edited. The first character typed makes it a cell with
 * text, where a comment starts from selected text as anywhere else; emptying the cell again brings the offer back.
 */

/** The open cell while it holds no text, or null: no cell open, or the open one has text. */
export function emptyOpenCell(state: EditorState) {
	const cell = state.field(openCellState, false) ?? null;
	return cell && cellSource(state, cell).trim() === '' ? cell : null;
}

/** Which empty cell is open, as a key that changes when another cell opens, so a move between empty cells raises the offer again. */
export const emptyCellKey = (state: EditorState) => {
	const cell = emptyOpenCell(state);
	return cell ? `${cell.tableFrom}:${cell.row}:${cell.col}` : null;
};

/**
 * The whole-cell selection the Comment offer is raised for while the open cell is empty, placed on that cell: above its top
 * edge, from its left. Null otherwise. A caret raises this rather than nothing, in the editor's own selection flow and in the widget comment
 * flow alike, so whichever reports last the answer is the same.
 */
export function emptyCellSelection(view: EditorView): WidgetSelection | null {
	const cell = emptyOpenCell(view.state);
	const widget = cell && drawnTableAt(view, cell.tableFrom);
	const td = widget && cellElement(widget, cell);
	if (!widget || !td) return null;
	// The same range a tap on an empty cell selects: the cell's contents, which for an empty cell is a caret at its start.
	const range = document.createRange();
	range.selectNodeContents(td);
	if (!range.collapsed) range.collapse(true);
	const raised = selectionIn(view, widget, range);
	if (!raised) return null;
	// That caret's own box has no size, so the offer is placed on the cell's box, the one its open editor is laid over.
	const box = td.getBoundingClientRect();
	return { ...raised, top: box.top, left: box.left };
}
