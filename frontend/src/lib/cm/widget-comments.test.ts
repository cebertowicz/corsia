import { describe, expect, it } from 'vitest';
import { snappedTableRows, tableCellLabel, tableCellLabelAtDocument, tableCells } from './widget-cells';
import { touchMovedBeyond } from './widget-comments';

const TABLE = `| Name | Status | Note |
|---|---|---|
| Delta | Yes | ordinary |
|  | A very long first cell value | https://example.com/this-is-an-unbroken-url |`;

const cell = (row: number, col: number) => {
	const found = tableCells(TABLE).find((span) => span.row === row && span.col === col);
	if (!found) throw new Error(`missing ${row},${col}`);
	return found;
};

describe('touchMovedBeyond', () => {
	it('rejects a page or table scroll but allows a still tap', () => {
		expect(touchMovedBeyond({ x: 10, y: 10 }, { x: 10, y: 10 })).toBe(false);
		expect(touchMovedBeyond({ x: 10, y: 10 }, { x: 17, y: 10 })).toBe(true);
	});
});

describe('snappedTableRows', () => {
	it('expands a cross-row gesture to all cells in its complete rows', () => {
		const cells = tableCells(TABLE);
		const start = cells.find((span) => span.row === 1 && span.col === 1)!;
		const end = cells.find((span) => span.row === 2 && span.col === 1)!;
		const snapped = snappedTableRows(TABLE, [start, end])!;
		expect(snapped.rows).toBe(2);
		expect(snapped.cells.map((span) => [span.row, span.col])).toEqual([[1, 0], [1, 1], [1, 2], [2, 0], [2, 1], [2, 2]]);
		expect(TABLE.slice(snapped.from, snapped.to)).toContain('Delta | Yes | ordinary');
	});
});

describe('tableCellLabel', () => {
	it('names a cell from its row first cell and column header', () => {
		expect(tableCellLabel(TABLE, cell(1, 1), 'Yes')).toBe('“Yes” · Delta, Status');
	});

	it('uses the header alone for the first column and row number for an empty first cell', () => {
		expect(tableCellLabel(TABLE, cell(1, 0), 'Delta')).toBe('“Delta” · Name');
		expect(tableCellLabel(TABLE, cell(2, 2), 'https://example.com/this-is-an-unbroken-url')).toBe('“https://example.com/this-is-an-unbroken-url” · row 2, Note');
	});

	it('derives a placed cell comment label from document offsets without storing it', () => {
		const document = `before\n\n${TABLE}\n\nafter`;
		const from = document.indexOf('Yes');
		expect(tableCellLabelAtDocument(document, from, from + 3, 'Yes')).toBe('“Yes” · Delta, Status');
		expect(tableCellLabelAtDocument(document, 0, 3, 'bef')).toBeNull();
	});

	it('truncates each displayed location name at 24 characters', () => {
		const source = '| Name | This is a deliberately long header |\n|---|---|\n| Delta | Yes |';
		const span = tableCells(source).find((item) => item.row === 1 && item.col === 1)!;
		expect(tableCellLabel(source, span, 'Yes')).toBe('“Yes” · Delta, This is a deliberately …');
	});
});
