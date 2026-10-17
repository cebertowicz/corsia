import { plainQuote } from '$lib/plainQuote';

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

/** The presentation label for a placed cell comment, reconstructed from its ordinary source offsets. */
export function tableCellLabelAtDocument(text: string, from: number | undefined, to: number | undefined, quote: string): string | null {
	if (from === undefined || to === undefined || from < 0 || to < from) return null;
	const lines: Array<{ from: number; to: number; text: string }> = [];
	for (let start = 0; start <= text.length;) {
		const end = text.indexOf('\n', start), to = end < 0 ? text.length : end;
		lines.push({ from: start, to, text: text.slice(start, to) });
		if (end < 0) break;
		start = end + 1;
	}
	const index = lines.findIndex((line) => from >= line.from && from <= line.to);
	if (index < 0 || !lines[index].text.includes('|')) return null;
	let first = index, last = index;
	while (first > 0 && lines[first - 1].text.includes('|')) first--;
	while (last + 1 < lines.length && lines[last + 1].text.includes('|')) last++;
	const blockFrom = lines[first].from, blockTo = lines[last].to;
	const src = text.slice(blockFrom, blockTo), relativeFrom = from - blockFrom, relativeTo = to - blockFrom;
	const cell = tableCells(src).find((span) => span.from < relativeTo && span.to > relativeFrom);
	return cell ? tableCellLabel(src, cell, quote) : null;
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
