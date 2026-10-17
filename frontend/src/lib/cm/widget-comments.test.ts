import { describe, expect, it } from 'vitest';
import { markdown } from '@codemirror/lang-markdown';
import { EditorState } from '@codemirror/state';
import { GFM } from '@lezer/markdown';
import { snappedTableRows, tableCellLabel, tableCells, tableCommentLabel } from './widget-cells';
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

// The editor's own parser setup (`MarkdownEditor.svelte`), the state a label is read from.
const documentState = (text: string) => EditorState.create({ doc: text, extensions: [markdown({ extensions: GFM })] });
const label = (text: string, from: number, to: number, quote: string) => tableCommentLabel(documentState(text), from, to, quote);
const at = (text: string, part: string) => { const from = text.indexOf(part); return [from, from + part.length] as const; };

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
		expect(label(document, from, from + 3, 'Yes')).toBe('“Yes” · Delta, Status');
		expect(label(document, 0, 3, 'bef')).toBeNull();
	});

	it('truncates each displayed location name at 24 characters', () => {
		const source = '| Name | This is a deliberately long header |\n|---|---|\n| Delta | Yes |';
		const span = tableCells(source).find((item) => item.row === 1 && item.col === 1)!;
		expect(tableCellLabel(source, span, 'Yes')).toBe('“Yes” · Delta, This is a deliberately …');
	});
});

describe('tableCommentLabel reads the table the widget draws', () => {
	it('gives a pipe in inline code in prose its plain quote', () => {
		const text = 'Run `cat x | grep y` now';
		expect(label(text, ...at(text, 'now'), 'now')).toBeNull();
	});

	it('keeps header and cell labels right under a pipe-bearing line directly above a table', () => {
		// Blank-line separated, the table is drawn and its cells are labelled from its own header. Adjacent, the parser reads one
		// paragraph (no widget), so the old pipe-run scan's labels there named cells that are not drawn.
		const text = `see a | b\n\n${TABLE}`;
		expect(label(text, ...at(text, 'Yes'), 'Yes')).toBe('“Yes” · Delta, Status');
		expect(label(text, ...at(text, 'Name'), 'Name')).toBe('“Name” · Name');
		expect(label(text, ...at(text, 'see a'), 'see a')).toBeNull();
		const adjacent = `see a | b\n${TABLE}`;
		expect(label(adjacent, ...at(adjacent, 'Yes'), 'Yes')).toBeNull();
	});

	it('gives text in a fenced code block that contains pipes its plain quote', () => {
		const text = '```\n| Name | Status |\n|---|---|\n| Delta | Yes |\n```\n';
		expect(label(text, ...at(text, 'Yes'), 'Yes')).toBeNull();
	});

	it('names a multi-row and a whole-table range as the composer does', () => {
		const text = `intro\n\n${TABLE}\n\nafter`, start = text.indexOf(TABLE);
		const rows = snappedTableRows(TABLE, [cell(1, 1), cell(2, 1)])!;
		// The composer raises the snapped source range of its rows; the card is handed that range back from storage.
		expect(label(text, start + rows.from, start + rows.to, text.slice(start + rows.from, start + rows.to))).toBe('2 rows, from “Delta”');
		expect(label(text, start, start + TABLE.length, TABLE)).toBe('The whole table');
	});

	it('names several cells of one row', () => {
		const [from] = at(TABLE, 'Delta'), [, to] = at(TABLE, 'ordinary');
		expect(label(TABLE, from, to, TABLE.slice(from, to))).toBe('3 cells, from “Delta”');
	});
});
