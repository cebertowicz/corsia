import type { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { blockRange, cellElement, drawnTableAt } from './widget-blocks';
import { CELL_EDITOR, cellSource, openCellState } from './widget-cell-state';
import { tableCells } from './widget-cells';
import { rowsBox, selectionIn, textPlace } from './widget-selection';
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

/**
 * Where the Comment offer sits for text selected in the open cell: measured off the cell editor's own text the way a drawn
 * cell's selection is (`textPlace`), since the table hides that source in the document. Null when the range is not in the open cell.
 */
export function openCellPlace(view: EditorView, from: number, to: number): { top: number; left: number } | null {
	const cell = view.state.field(openCellState, false);
	if (!cell || from < cell.from || to > cell.to) return null;
	const el = view.dom.querySelector<HTMLElement>(`${CELL_EDITOR} .cm-editor`);
	const inner = el && EditorView.findFromDOM(el);
	if (!inner) return null;
	const start = inner.domAtPos(from - cell.from), end = inner.domAtPos(to - cell.from), range = document.createRange();
	range.setStart(start.node, start.offset);
	range.setEnd(end.node, end.offset);
	return textPlace(range);
}

/** The box a comment's composer is anchored to for a range inside one table cell, open or not: its row, as for a drawn cell's selection (`rowsBox`). */
export function cellRowsAt(view: EditorView, from: number, to: number): DOMRect | null {
	const widget = drawnTableAt(view, from), block = widget && blockRange(view, widget);
	if (!widget || !block || to > block.to) return null;
	const span = tableCells(view.state.sliceDoc(block.from, block.to))
		.find((s) => from - block.from >= s.rawFrom && to - block.from <= s.rawTo);
	const td = span && cellElement(widget, span);
	return td ? rowsBox(widget, [td]) : null;
}
