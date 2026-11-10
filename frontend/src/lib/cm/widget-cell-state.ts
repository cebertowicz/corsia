import { StateEffect, StateField, type EditorState } from '@codemirror/state';
import { tableAt } from './widget-blocks';
import { intersectsCell, tableCells, type CellSpan } from './widget-cells';

/**
 * Which table cell is being edited, and where its text lives in the document.
 *
 * The cell is tracked twice over: its range is mapped through every main-document transaction, and on each change that
 * mapped range is re-derived from the editor's own parse (`tableAt` for the table's lines, `tableCells` for the cell's
 * place in them). Mapping alone follows the text; the re-derivation keeps the range on the cell's own pipes when a
 * collaborator's update moves a row or a column, and answers "no cell here" — which closes the editor — when the cell or
 * its table is gone.
 */

/** The cell under edit: its table's source start, its place in the drawn table, and the source range its editor holds. */
export interface OpenCell {
	tableFrom: number;
	row: number;
	col: number;
	from: number;
	to: number;
	/** A cell the renderer pads in has no source of its own, so its first character has to open the cell in the source. */
	padded: boolean;
}

export const openCellAt = StateEffect.define<OpenCell>();
export const closeCell = StateEffect.define<null>();

/** The cell's whole source between its two pipes, padding included: what the cell editor holds, byte for byte. */
export const cellSource = (state: EditorState, cell: OpenCell) => state.sliceDoc(cell.from, cell.to);

/** Whether the source really delimits this cell, as opposed to the renderer padding an empty one in at the row's end. */
const delimited = (state: EditorState, cell: { from: number; to: number }) =>
	state.sliceDoc(cell.from - 1, cell.from) === '|' && state.sliceDoc(cell.to, cell.to + 1) === '|';

/** The cell the source span describes, resolved against the document as it stands. */
export const cellAtSpan = (state: EditorState, block: number, span: CellSpan): OpenCell => ({
	tableFrom: block,
	row: span.row,
	col: span.col,
	from: block + span.rawFrom,
	to: block + span.rawTo,
	padded: !delimited(state, { from: block + span.rawFrom, to: block + span.rawTo }),
});

/**
 * The cell the mapped range best covers, from the editor's own parse. Raw spans (the pipes' inner edges) rather than the
 * trimmed content ones are what the editor can hold: a space typed at the edge of a cell has to land somewhere. An empty
 * cell has no width to overlap, so a range that has collapsed onto its source anchor is still that cell's.
 */
export function resolveCell(state: EditorState, cell: OpenCell): OpenCell | null {
	const block = tableAt(state, cell.from, Math.max(cell.from, cell.to));
	if (!block) return null;
	const spans = tableCells(state.sliceDoc(block.from, block.to));
	const from = cell.from - block.from, to = Math.max(from, cell.to - block.from);
	let best: { span: CellSpan; over: number } | null = null;
	for (const span of spans) {
		const over = Math.min(span.rawTo, to) - Math.max(span.rawFrom, from);
		if (over > 0 && (!best || over > best.over)) best = { span, over };
	}
	const span = best?.span
		?? spans.find((item) => intersectsCell(item, from, to))
		// A zero-width range inside the cell's own span: an empty cell the reader has just opened.
		?? spans.find((item) => item.rawFrom <= from && to <= item.rawTo);
	if (!span) return null;
	return cellAtSpan(state, block.from, span);
}

/** The cell to open from a click on the drawn cell at `row`/`col` of the table that starts at `tableFrom`. */
export function cellToOpen(state: EditorState, tableFrom: number, row: number, col: number): OpenCell | null {
	const src = state.sliceDoc(tableFrom, state.doc.length);
	const span = tableCells(src).find((item) => item.row === row && item.col === col);
	if (!span) return null;
	return resolveCell(state, cellAtSpan(state, tableFrom, span));
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
		// Outward at both ends: text inserted at a cell's own edge — typed at its start, or the delimiters materialised
		// around a cell the source did not have yet — belongs to that cell, so the range grows instead of stepping past it.
		return resolveCell(transaction.state, {
			...value,
			tableFrom: changes.mapPos(value.tableFrom, 1),
			from: changes.mapPos(value.from, -1),
			to: changes.mapPos(value.to, 1)
		});
	}
});

/**
 * A cell the renderer pads in has no delimiters of its own yet, so the text typed into it opens the cell in the
 * source: the row gains its pipe and the text sits between the two, inside the one transaction the reader made.
 */
export function openCellInSource(state: EditorState, changes: { from: number; insert: string }[], cell: OpenCell) {
	const leading = state.sliceDoc(cell.from - 1, cell.from) === '|' ? ' ' : ' | ';
	const first = changes.find((change) => change.insert && change.from === cell.from);
	if (first) first.insert = `${leading}${first.insert} |`;
}

/** The cell editor's write of the document's cell text back into the cell editor: never sent back, never rewritten. */
export const CELL_SYNC = 'table-cell-sync';

/** The class of the open cell editor's element, in a layer over the cell: an event from inside it is the editor's, not the table's. */
export const CELL_EDITOR = '.cm-md-cell-editor';
