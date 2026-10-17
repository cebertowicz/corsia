import type { EditorState } from '@codemirror/state';
import { plainQuote } from '$lib/plainQuote';
import { tableAt } from './widget-blocks';

const SNIPPET = 64;

export interface CellSpan { row: number; col: number; from: number; to: number }

/** The cells of a GFM table's source, as offsets into it: trimmed content spans, the delimiter row skipped. */
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
			const raw = line.slice(edges[i] + 1, edges[i + 1]);
			const from = edges[i] + 1 + (raw.length - raw.trimStart().length);
			cells.push({ row, col: col++, from: start + from, to: start + from + raw.trim().length });
		}
	});
	return cells;
}

export const snippet = (value: string) => value.length > SNIPPET ? `${value.slice(0, SNIPPET)}…` : value;
export const clean = (value: string) => value.replace(/\s+/g, ' ').trim();
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
	if (cell.col === 0) return `“${plainQuote(quote)}” · ${column}`;
	const row = cellName(first ? src.slice(first.from, first.to) : '') || `row ${cell.row}`;
	return `“${plainQuote(quote)}” · ${row}, ${column}`;
}

/**
 * The one label for a comment on a table's text, shared by the composer and the card: derived from the source offsets
 * and the syntax-tree table they sit in (the editor's own tree), never persisted. Null for anything outside a table, and for a range that
 * runs past one, so prose or code that contains a pipe, or a selection that merely includes a table, keeps its ordinary quote.
 */
export function tableCommentLabel(state: EditorState, from: number | undefined, to: number | undefined, quote: string): string | null {
	if (from === undefined || to === undefined || from < 0 || to < from || to > state.doc.length) return null;
	const table = tableAt(state, from, to);
	if (!table) return null;
	if (from < table.from || to > table.to) return null;
	const src = state.sliceDoc(table.from, table.to), cells = tableCells(src);
	const hit = cells.filter((span) => span.to > span.from && span.from < to - table.from && span.to > from - table.from);
	if (hit.length === 0) return null;
	if (hit.length === cells.filter((span) => span.to > span.from).length) return 'The whole table';
	const rows = new Set(hit.map((span) => span.row)).size;
	const named = (span: CellSpan) => snippet(clean(plainQuote(src.slice(span.from, span.to))));
	if (rows > 1) return `${rows} rows, from “${named(cells.find((span) => span.row === hit[0].row && span.col === 0) ?? hit[0])}”`;
	if (hit.length > 1) return `${hit.length} cells, from “${named(hit[0])}”`;
	return tableCellLabel(src, hit[0], quote);
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
