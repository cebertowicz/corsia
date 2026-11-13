import { EditorSelection, EditorState, Transaction } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { tableAt } from './widget-blocks';
import { clean, tableCells, tableGrid, type CellSpan } from './widget-cells';
import { cellAtSpan, cellSource, cellWrite, openCellAt, type OpenCell } from './widget-cell-state';
import { heldText, heldToSource, sourceOffset, sourceToHeld, writeCellEdit } from './widget-cell-row';

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
 * Where an insertion at `at` sits against a table ending directly above it. `above` counts the line breaks already between
 * them: 1 on the line right under the table's last row, 0 at the end of that row when it ends the document (a document
 * ending in a table with no newline). `onBlock` is true when that line is not blank but the start of a block that ends the
 * table with no blank line between (a heading, list item, blockquote or fence). Null when no table ends directly above.
 */
function belowTable(state: EditorState, at: number): { above: number; onBlock: boolean } | null {
	const line = state.doc.lineAt(at);
	const onBlock = at === line.from && !!line.text.trim();
	if (line.number > 1 && (onBlock || !line.text.trim())) {
		const above = state.doc.line(line.number - 1);
		return tableAt(state, above.from, above.to)?.to === above.to ? { above: 1, onBlock } : null;
	}
	return at === state.doc.length && tableAt(state, line.from, line.to)?.to === at ? { above: 0, onBlock: false } : null;
}

/**
 * Text typed on the line directly under a table becomes its own paragraph. Leaving a table (Escape, Tab, Enter, a click
 * or an arrow key) can put the caret there, and GFM, like the editor's own parse, reads a line right under a row as another
 * row (or, past the header's width, as a cell that is never drawn), and a paragraph right under that line joins it. That
 * line can also be the start of a heading, list item, blockquote or fence that ends the table with no blank line, where a
 * typed character turns the block's first line into a row. So the text goes in with a blank line above it, and a blank
 * line below it when a block follows, leaving the table and that block as they were. The separators are added only when
 * something is typed, pasted or composed there, never when the reader only arrives, so arriving changes nothing in the
 * document; they go in the same transaction as the text, so one undo takes all of it out. A cell's own write into its row
 * (`cellWrite`) is left alone: it keeps the row a row, even where the row ends the document.
 */
export const typingBelowTable = EditorState.transactionFilter.of((tr) => {
	if (!tr.docChanged || !tr.isUserEvent('input') || tr.annotation(cellWrite)) return tr;
	const state = tr.startState;
	let at = -1, insert = '', count = 0;
	tr.changes.iterChanges((fromA, toA, _fromB, _toB, text) => { count++; if (fromA === toA) { at = fromA; insert = text.toString(); } });
	const text = insert.replace(/^\n+/, '');
	if (count !== 1 || at < 0 || !text.replace(/\n+$/, '')) return tr;
	const below = belowTable(state, at);
	if (!below) return tr;
	const { above, onBlock } = below;
	const lead = insert.length - text.length, trail = text.length - text.replace(/\n+$/, '').length;
	const before = '\n'.repeat(Math.max(0, 2 - above - lead));
	// The block the caret stands at the start of needs a blank line before it. On a blank line, a block on the next line
	// would join the typed line; the rest of the caret's line is blank, and its own line break counts as one of the two.
	const line = state.doc.lineAt(at);
	const next = line.number < state.doc.lines ? state.doc.line(line.number + 1) : null;
	const breaks = onBlock ? 2 : next && next.text.trim() && above === 1 ? 1 : 0;
	const after = '\n'.repeat(Math.max(0, breaks - trail));
	if (!before && !after) return tr;
	const end = at + insert.length;
	const shift = (pos: number) => pos < at ? pos : pos + before.length + (pos > end ? after.length : 0);
	const selection = tr.selection && EditorSelection.create(
		tr.selection.ranges.map((range) => EditorSelection.range(shift(range.anchor), shift(range.head))), tr.selection.mainIndex);
	const event = tr.annotation(Transaction.userEvent);
	return {
		changes: { from: at, insert: before + insert + after },
		...(selection ? { selection } : {}),
		effects: tr.effects,
		scrollIntoView: tr.scrollIntoView,
		...(event ? { userEvent: event } : {})
	};
});

/** The drawn cell nearest document position `pos`, in the table whose source holds it: the cell holding it, or the closest. Null outside every table. */
function nearestCell(state: EditorState, pos: number): OpenCell | null {
	const block = tableAt(state, pos, pos);
	if (!block) return null;
	let nearest: OpenCell | null = null, gap = Infinity;
	for (const span of tableGrid(state.sliceDoc(block.from, block.to))) {
		if (span.padded) continue;
		const cell = cellAtSpan(block.from, span), away = Math.max(cell.from - pos, pos - cell.to, 0);
		if (away < gap) { nearest = cell; gap = away; }
	}
	return nearest;
}

/**
 * Text typed in the document's own editor while its caret is in a table's source: the cell was left by a click outside the
 * document, or a card gave the keyboard back to the document. The table is drawn in place of its source, so the browser
 * has no caret to show there and puts the text after the table, as a paragraph, or as a row that pulls the paragraph below
 * into the table. It goes into the cell nearest the caret instead, written into the row as the cell's editor writes it,
 * and that cell opens with the caret after it, so the rest of the typing goes there too.
 */
export const typingInTableSource = EditorView.inputHandler.of((view, _from, _to, text) => {
	const { main } = view.state.selection;
	// The end of a table that ends the document is where leaving it puts the caret; typing there is `typingBelowTable`'s.
	const cell = !view.composing && main.empty && !belowTable(view.state, main.head) ? nearestCell(view.state, main.head) : null;
	if (!cell) return false;
	const line = view.state.doc.lineAt(cell.rowStart), source = cellSource(view.state, cell), held = heldText(source);
	const at = sourceToHeld(source, held, Math.min(Math.max(main.head, cell.from), cell.to) - cell.from), edited = heldToSource(held, at);
	const write = writeCellEdit(line.text, cell.col, held.text.slice(0, at) + text + held.text.slice(at), { from: edited, to: edited });
	// The written cell's source is its content between one space each side.
	const span = tableCells(write.row).find((item) => item.col === cell.col);
	const caret = line.from + (span ? span.rawFrom + 1 + sourceOffset(write.text, write.at(at + text.length)) : write.row.length);
	view.dispatch({
		changes: write.changes.map((change) => ({ ...change, from: line.from + change.from, to: line.from + change.to })),
		selection: { anchor: caret }, effects: openCellAt.of(cell), annotations: cellWrite.of(true), userEvent: 'input.type'
	});
	return true;
});

/** The table block the open cell is in, for leaving it. */
export const tableOfCell = (state: EditorState, cell: OpenCell) => tableAt(state, cell.from, cell.from + 1);
