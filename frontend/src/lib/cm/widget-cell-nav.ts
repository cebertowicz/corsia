import { EditorSelection, EditorState, Transaction } from '@codemirror/state';
import { tableAt } from './widget-blocks';
import { clean, tableGrid, type CellSpan } from './widget-cells';
import { cellAtSpan, openCellState, type OpenCell } from './widget-cell-state';

/**
 * What a screen reader calls the open cell's editor: its column's header and its body row, counted from 1, or the column's
 * number when its header is empty. A header cell has no row of its own to name, so it is named as its column's header.
 */
export function cellLabel(td: HTMLTableCellElement): string {
	const row = td.parentElement instanceof HTMLTableRowElement ? td.parentElement.rowIndex : 0, col = td.cellIndex + 1;
	if (row === 0) return `Column ${col} header`;
	const header = clean(td.closest('table')?.rows[0]?.cells[td.cellIndex]?.textContent ?? '');
	return `${header || `Column ${col}`}, row ${row}`;
}

/**
 * Where Tab, Shift+Tab and Enter take the reader inside a table. The walk is over the source cells of the open cell's own
 * table, in the order they are drawn, so a padded empty cell is a stop like any other.
 */

function tableOf(state: EditorState, cell: OpenCell): { from: number; to: number; cells: CellSpan[] } | null {
	const block = tableAt(state, cell.from, cell.from + 1);
	if (!block) return null;
	return { ...block, cells: tableGrid(state.sliceDoc(block.from, block.to)) };
}

const stepTo = (block: { from: number }, cells: CellSpan[], index: number): OpenCell | null =>
	index < 0 || index >= cells.length ? null : cellAtSpan(block.from, cells[index]);

/** Tab and Shift+Tab: the next or previous cell, running on from the end of one row to the start of the next. Null when there is no cell left to move to. */
export function adjacentCell(state: EditorState, cell: OpenCell, step: 1 | -1): OpenCell | null {
	const table = tableOf(state, cell);
	if (!table) return null;
	const index = table.cells.findIndex((span) => span.row === cell.row && span.col === cell.col);
	if (index < 0) return null;
	return stepTo(table, table.cells, index + step);
}

/** Enter: the same column one row down. Null on the last row, where there is nowhere below to go. */
export function cellBelow(state: EditorState, cell: OpenCell): OpenCell | null {
	const table = tableOf(state, cell);
	if (!table) return null;
	const below = table.cells.find((span) => span.row === cell.row + 1 && span.col === cell.col);
	return below ? cellAtSpan(table.from, below) : null;
}

/** Where the caret goes when the reader leaves the table: the start of the line below it, or the end of the document. */
export function caretAfterTable(state: EditorState, tableTo: number): number {
	if (tableTo >= state.doc.length) return state.doc.length;
	return state.doc.lineAt(Math.min(tableTo + 1, state.doc.length)).from;
}

/**
 * Text typed at the end of a document that ends in a table goes below a blank line. Leaving the table puts the caret
 * there, and GFM, like the editor's own parse, reads a line right under a row as another row of the table (or, past the
 * header's width, as a cell that is never drawn). The separator is added only when something is typed, pasted or composed
 * there with no cell open, never when the reader only leaves, so leaving changes nothing in the document; it goes in the
 * same transaction as the text, so one undo takes both out.
 */
export const typingBelowFinalTable = EditorState.transactionFilter.of((tr) => {
	if (!tr.docChanged || !tr.isUserEvent('input') || tr.startState.field(openCellState, false)) return tr;
	const state = tr.startState, end = state.doc.length;
	let insert = '', count = 0;
	tr.changes.iterChanges((fromA, toA, _fromB, _toB, text) => { count++; if (fromA === end && toA === end) insert = text.toString(); });
	const text = insert.replace(/^\n+/, '');
	if (count !== 1 || !text) return tr;
	// The last line holding anything, and the blank lines after it.
	let line = state.doc.lineAt(end);
	while (!line.text.trim() && line.number > 1) line = state.doc.line(line.number - 1);
	const table = tableAt(state, line.from, line.to);
	if (!table || table.to !== line.to) return tr;
	const newlines = state.doc.sliceString(table.to, end).split('\n').length - 1 + (insert.length - text.length);
	if (newlines >= 2) return tr;
	const separator = '\n'.repeat(2 - newlines);
	const shift = (pos: number) => pos >= end ? pos + separator.length : pos;
	const selection = tr.selection && EditorSelection.create(
		tr.selection.ranges.map((range) => EditorSelection.range(shift(range.anchor), shift(range.head))), tr.selection.mainIndex);
	const event = tr.annotation(Transaction.userEvent);
	return {
		changes: { from: end, insert: separator + insert },
		...(selection ? { selection } : {}),
		effects: tr.effects,
		scrollIntoView: tr.scrollIntoView,
		...(event ? { userEvent: event } : {})
	};
});

/** The table block the open cell is in, for leaving it. */
export const tableOfCell = (state: EditorState, cell: OpenCell) => tableAt(state, cell.from, cell.from + 1);
