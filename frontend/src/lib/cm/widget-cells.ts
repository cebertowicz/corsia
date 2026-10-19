import type { EditorState } from '@codemirror/state';
import { plainQuote } from '$lib/plainQuote';
import { codeTextAt, diagramAt, tableAt } from './widget-blocks';

const SNIPPET = 64;

export interface CellSpan { row: number; col: number; from: number; to: number; anchorFrom: number; anchorTo: number }

/** The cells of a GFM table's source, as offsets into it: trimmed content spans and a non-empty source anchor for empty cells; the delimiter row is skipped. */
export function tableCells(src: string): CellSpan[] {
	const cells: CellSpan[] = [];
	let lineStart = 0;
	src.split('\n').forEach((line, index) => {
		const start = lineStart;
		lineStart += line.length + 1;
		if (index === 1) return; // the delimiter row
		const row = index === 0 ? 0 : index - 1;
		const bars: number[] = [];
		for (let i = 0; i < line.length; i++) {
			if (line[i] === '\\') i++;
			else if (line[i] === '|') bars.push(i);
		}
		const edges = [-1, ...bars, line.length];
		const lead = line.slice(0, bars[0] ?? 0).trim() === '' && bars.length > 0;
		const tail = bars.length > 0 && line.slice(bars[bars.length - 1] + 1).trim() === '';
		let col = 0;
		for (let i = 0; i < edges.length - 1; i++) {
			if ((i === 0 && lead) || (i === edges.length - 2 && tail)) continue;
			const rawStart = edges[i] + 1, rawEnd = edges[i + 1], raw = line.slice(rawStart, rawEnd);
			const from = rawStart + (raw.length - raw.trimStart().length), to = from + raw.trim().length;
			// An empty rendered cell has no DOM text to select. Its source whitespace is a stable anchor; when it has none,
			// its left delimiter is the smallest non-empty source span that still belongs uniquely to this cell.
			const anchorFrom = from === to ? (rawStart < rawEnd ? rawStart : Math.max(0, rawStart - 1)) : from;
			const anchorTo = from === to ? (rawStart < rawEnd ? rawEnd : rawStart) : to;
			cells.push({ row, col: col++, from: start + from, to: start + to, anchorFrom: start + anchorFrom, anchorTo: start + anchorTo });
		}
	});
	return cells;
}

export const snippet = (value: string) => value.length > SNIPPET ? `${value.slice(0, SNIPPET)}…` : value;
export const clean = (value: string) => value.replace(/\s+/g, ' ').trim();

/** Whether a source range touches the visible cell, including an empty cell's structural source anchor. */
export const intersectsCell = (span: CellSpan, from: number, to: number) =>
	span.anchorFrom < to && span.anchorTo > from;
const cellName = (value: string) => {
	const text = plainQuote(clean(value));
	return text.length > 24 ? `${text.slice(0, 23)}…` : text;
};

/** Display-only cell context; source offsets remain the one stored comment anchor. */
export function tableCellLabel(src: string, cell: CellSpan, quote: string): string {
	const cells = tableCells(src);
	const header = cells.find((span) => span.row === 0 && span.col === cell.col);
	const first = cells.find((span) => span.row === cell.row && span.col === 0);
	const column = cellName(header ? src.slice(header.from, header.to) : `column ${cell.col + 1}`);
	const selected = plainQuote(cell.from === cell.to ? 'Empty cell' : quote.trim() || 'Empty cell');
	if (cell.col === 0) return `“${selected}” · ${column}`;
	const row = cellName(first ? src.slice(first.from, first.to) : '') || `row ${cell.row}`;
	return `“${selected}” · ${row}, ${column}`;
}

/**
 * The one label for a comment on a table's or diagram's text, shared by the composer and the card: derived from the source offsets
 * and the syntax-tree table or diagram they sit in (the editor's own tree), never persisted. A diagram is labelled only for the whole block.
 * Null for anything outside a table, and for a range that runs past one, so prose or code that contains a pipe, or a selection that
 * merely includes a table, keeps its ordinary quote.
 */
export function widgetCommentLabel(state: EditorState, from: number | undefined, to: number | undefined, quote: string): string | null {
	if (from === undefined || to === undefined || from < 0 || to < from || to > state.doc.length) return null;
	if (diagramAt(state, from, to)) return 'The whole diagram';
	const table = tableAt(state, from, to);
	if (!table) return null;
	if (from < table.from || to > table.to) return null;
	const src = state.sliceDoc(table.from, table.to), cells = tableCells(src);
	const hit = cells.filter((span) => intersectsCell(span, from - table.from, to - table.from));
	if (hit.length === 0) return null;
	if (hit.length === cells.length) return 'The whole table';
	const rows = new Set(hit.map((span) => span.row)).size;
	const named = (span: CellSpan) => snippet(clean(plainQuote(src.slice(span.from, span.to))));
	if (rows > 1) return `${rows} rows, from “${named(cells.find((span) => span.row === hit[0].row && span.col === 0) ?? hit[0])}”`;
	if (hit.length > 1) return `${hit.length} cells, from “${named(hit[0])}”`;
	return tableCellLabel(src, hit[0], quote);
}

/**
 * What a comment's card shows in place of its plain quote: the widget label, or for a range inside a code span's or block's text the
 * quote as written, since code has no inline marks to drop. Null keeps the plain quote.
 */
export function cardQuoteLabel(state: EditorState, from: number | undefined, to: number | undefined, quote: string): string | null {
	const label = widgetCommentLabel(state, from, to, quote);
	if (label !== null || from === undefined || to === undefined || from < 0 || to < from || to > state.doc.length) return label;
	return codeTextAt(state, from, to) ? `“${quote.trim()}”` : null;
}

/** The full contiguous source range and cells for a selection crossing one or more table rows. */
export function snappedTableRows(src: string, covered: CellSpan[]): { from: number; to: number; rows: number; cells: CellSpan[] } | null {
	if (covered.length === 0) return null;
	const rowNumbers = [...new Set(covered.map((span) => span.row))];
	const firstRow = Math.min(...rowNumbers), lastRow = Math.max(...rowNumbers);
	const cells = tableCells(src).filter((span) => span.row >= firstRow && span.row <= lastRow);
	return cells.length ? { from: cells[0].from, to: cells[cells.length - 1].to, rows: lastRow - firstRow + 1, cells } : null;
}

export function occurrences(text: string, part: string): number[] {
	const found: number[] = [];
	for (let at = text.indexOf(part); part && at >= 0; at = text.indexOf(part, at + 1)) found.push(at);
	return found;
}
