import type { EditorState } from '@codemirror/state';
import { tableAt } from './widget-blocks';
import { tableGrid, type CellSpan } from './widget-cells';
import { cellAtSpan, type OpenCell } from './widget-cell-state';

/**
 * Where Tab, Shift+Tab and Enter take the reader inside a table. The walk is over the source cells of the open cell's own
 * table, in the order they are drawn, so a padded empty cell is a stop like any other.
 */

function tableOf(state: EditorState, cell: OpenCell): { from: number; to: number; cells: CellSpan[] } | null {
	const block = tableAt(state, cell.from, cell.from + 1);
	if (!block) return null;
	return { ...block, cells: tableGrid(state.sliceDoc(block.from, block.to)) };
}

const stepTo = (block: { from: number }, state: EditorState, cells: CellSpan[], index: number): OpenCell | null =>
	index < 0 || index >= cells.length ? null : cellAtSpan(state, block.from, cells[index]);

/** Tab and Shift+Tab: the next or previous cell, running on from the end of one row to the start of the next. Null when there is no cell left to move to. */
export function adjacentCell(state: EditorState, cell: OpenCell, step: 1 | -1): OpenCell | null {
	const table = tableOf(state, cell);
	if (!table) return null;
	const index = table.cells.findIndex((span) => span.row === cell.row && span.col === cell.col);
	if (index < 0) return null;
	return stepTo(table, state, table.cells, index + step);
}

/** Enter: the same column one row down. Null on the last row, where there is nowhere below to go. */
export function cellBelow(state: EditorState, cell: OpenCell): OpenCell | null {
	const table = tableOf(state, cell);
	if (!table) return null;
	const below = table.cells.find((span) => span.row === cell.row + 1 && span.col === cell.col);
	return below ? cellAtSpan(state, table.from, below) : null;
}

/** Where the caret goes when the reader leaves the table: the start of the line below it, or the end of the document. */
export function caretAfterTable(state: EditorState, tableTo: number): number {
	if (tableTo >= state.doc.length) return state.doc.length;
	return state.doc.lineAt(Math.min(tableTo + 1, state.doc.length)).from;
}

/** The table block the open cell is in, for leaving it. */
export const tableOfCell = (state: EditorState, cell: OpenCell) => tableAt(state, cell.from, cell.from + 1);
