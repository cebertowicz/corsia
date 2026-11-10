import { describe, expect, it } from 'vitest';
import { tableCells } from './widget-cells';

/**
 * The one table-source parser, shared by the cell editor, the comment anchor mapping and the paint pass. GFM renders every
 * row to the header's column count, so every rendered `<td>` — including a padded empty one a person can click — has to
 * answer to a span, or that cell can be neither edited nor commented.
 */

const place = (src: string) => tableCells(src).map((span) => `${span.row}:${span.col}`).join(' ');

describe('the cells of a table source', () => {
	it('gives one span per cell of a full row', () => {
		const src = '| a | b |\n| --- | --- |\n| c | d |';
		expect(place(src)).toBe('0:0 0:1 1:0 1:1');
		const [header] = tableCells(src);
		expect(src.slice(header.rawFrom, header.rawTo)).toBe(' a ');
		expect(src.slice(header.from, header.to)).toBe('a');
	});

	it('gives the empty last cell a span, even when it is written as two pipes', () => {
		const src = '| a | b |\n| --- | --- |\n| c ||';
		expect(place(src)).toBe('0:0 0:1 1:0 1:1');
		const cell = tableCells(src).find((span) => span.row === 1 && span.col === 1)!;
		expect(cell.from).toBe(cell.to);
		expect(cell.anchorTo, 'a collapsed range can still land on it').toBeGreaterThan(cell.anchorFrom);
	});

	it('pads a row shorter than its header so the empty cell the renderer draws has a span of its own', () => {
		const src = '| a | b |\n| --- | --- |\n| c |';
		expect(place(src), 'the render gives this row two cells').toBe('0:0 0:1 1:0 1:1');
		const cell = tableCells(src).find((span) => span.row === 1 && span.col === 1)!;
		expect(cell.rawFrom, 'it has no source of its own').toBe(cell.rawTo);
		expect(src.slice(cell.rawFrom, cell.rawTo)).toBe('');
		expect(src.slice(cell.anchorFrom, cell.anchorTo), 'its anchor is the row\'s last delimiter').toBe('|');
		const row = src.split('\n')[2];
		expect(cell.rawFrom, 'it sits at the row\'s end, where its text has to be inserted').toBe(src.length);
		expect(row.endsWith('|')).toBe(true);
	});

	it('pads a two-cell row of a three-column table', () => {
		expect(place('| a | b | c |\n| --- | --- | --- |\n| d | e |')).toBe('0:0 0:1 0:2 1:0 1:1 1:2');
	});

	it('keeps the spans of a row with more cells than its header, which the render drops', () => {
		const src = '| a | b |\n| --- | --- |\n| c | d | e |';
		expect(place(src)).toBe('0:0 0:1 1:0 1:1 1:2');
	});

	it('holds one row order across the padding, which the comment anchor mapping reads in order', () => {
		const src = '| a | b | c |\n| --- | --- | --- |\n| d |\n| e | f |';
		const cells = tableCells(src);
		expect(place(src)).toBe('0:0 0:1 0:2 1:0 1:1 1:2 2:0 2:1 2:2');
		expect(cells.every((span, index) => index === 0 || span.rawFrom >= cells[index - 1].rawFrom), 'never out of order').toBe(true);
	});
});
