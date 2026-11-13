import { StateEffect, StateField, type ChangeDesc, type EditorState, type Text } from '@codemirror/state';
import { tableAt } from './widget-blocks';
import { tableGrid, type CellSpan } from './widget-cells';

/**
 * Which table cell is being edited, and where its text lives in the document.
 *
 * A cell is named by its row's line and its column: the line's start is mapped through every main-document transaction,
 * and everything else (its table, its row index, its source range, whether the row pads it in) is read again from the
 * editor's own parse (`tableAt`, `tableGrid`) on that line. So an edit to another row moves the line and leaves the column,
 * and the answer "no such cell" — which closes the editor — comes when the row or its table is gone.
 */

/** The cell under edit: its row's line start and column, and, read from them, its table, row index and source range. */
export interface OpenCell {
	tableFrom: number;
	rowStart: number;
	row: number;
	col: number;
	from: number;
	to: number;
	/** A cell the renderer pads in has no source of its own: its range is empty, at the row's end. */
	padded: boolean;
}

export const openCellAt = StateEffect.define<OpenCell>();
export const closeCell = StateEffect.define<null>();

/** The cell's whole source between its two pipes, padding included. */
export const cellSource = (state: EditorState, cell: OpenCell) => state.sliceDoc(cell.from, cell.to);

/**
 * The cell the source span describes. Whether it is padded is the span's own flag, set by `tableGrid` where it pads a short
 * row: GFM leaves out a row's first or last pipe, so a cell's neighbouring pipes say nothing about it.
 */
export const cellAtSpan = (block: number, span: CellSpan): OpenCell => ({
	tableFrom: block,
	rowStart: block + span.rowFrom,
	row: span.row,
	col: span.col,
	from: block + span.rawFrom,
	to: block + span.rawTo,
	padded: span.padded === true,
});

/** The drawn cell in column `col` of the table row on the line starting at `rowStart`, from the editor's own parse; null when there is none. */
export function cellAt(state: EditorState, rowStart: number, col: number): OpenCell | null {
	const block = tableAt(state, rowStart, rowStart);
	if (!block) return null;
	const span = tableGrid(state.sliceDoc(block.from, block.to)).find((item) => block.from + item.rowFrom === rowStart && item.col === col);
	return span ? cellAtSpan(block.from, span) : null;
}

/**
 * Where a row's line starts after `changes`: text inserted at its start (a row added above, or the pipe a row without one
 * gains) is before it, so the start moves past it and back to the line's own start.
 */
export const mapRowStart = (changes: ChangeDesc, doc: Text, rowStart: number) => doc.lineAt(changes.mapPos(rowStart, 1)).from;

/**
 * Whether `changes` replaced text across one of the row's ends: the row was deleted, joined with a neighbour or rewritten
 * along with the rows around it. Nothing is left to say which cell was open then, so it closes; what was typed is already
 * in the document.
 */
function rowReplaced(changes: ChangeDesc, doc: Text, rowStart: number) {
	const end = doc.lineAt(rowStart).to;
	let replaced = false;
	changes.iterChangedRanges((fromA, toA) => {
		if ((fromA < rowStart && rowStart < toA) || (fromA < end && end < toA)) replaced = true;
		// A deletion that ends at the row's start from inside the line above, or starts at its end and stops inside the line
		// below, joins the two lines into one.
		else if (fromA < rowStart && toA === rowStart && doc.lineAt(fromA).from !== fromA) replaced = true;
		else if (fromA === end && toA > end && doc.lineAt(toA).to !== toA) replaced = true;
	});
	return replaced;
}

/** The drawn cell whose source holds document position `pos`, from the editor's own parse; null outside every cell. */
export function cellAtPosition(state: EditorState, pos: number): OpenCell | null {
	const block = tableAt(state, pos, pos);
	if (!block) return null;
	const span = tableGrid(state.sliceDoc(block.from, block.to)).find((item) => !item.padded && block.from + item.rawFrom <= pos && pos <= block.from + item.rawTo);
	return span ? cellAtSpan(block.from, span) : null;
}

/** The cell to open from a click on the drawn cell at `row`/`col` of the table that starts at `tableFrom`. */
export function cellToOpen(state: EditorState, tableFrom: number, row: number, col: number): OpenCell | null {
	const block = tableAt(state, tableFrom, tableFrom);
	if (!block) return null;
	const span = tableGrid(state.sliceDoc(block.from, block.to)).find((item) => item.row === row && item.col === col);
	return span ? cellAtSpan(block.from, span) : null;
}

export const openCellState = StateField.define<OpenCell | null>({
	create: () => null,
	update(value, transaction) {
		for (const effect of transaction.effects) {
			if (effect.is(openCellAt)) value = effect.value;
			else if (effect.is(closeCell)) value = null;
		}
		if (!value || !transaction.docChanged) return value;
		const { changes } = transaction;
		if (rowReplaced(changes, transaction.startState.doc, value.rowStart)) return null;
		return cellAt(transaction.state, mapRowStart(changes, transaction.newDoc, value.rowStart), value.col);
	}
});

/** The cell editor's write of the document's cell text back into the cell editor: never sent back, never rewritten. */
export const CELL_SYNC = 'table-cell-sync';

/** The class of the open cell editor's element, in place of the cell's drawn content: an event from inside it is the editor's, not the table's. */
export const CELL_EDITOR = '.cm-md-cell-editor';
