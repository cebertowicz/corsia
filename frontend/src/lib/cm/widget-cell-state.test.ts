import { describe, expect, it, vi } from 'vitest';
import { markdown } from '@codemirror/lang-markdown';
import { EditorState } from '@codemirror/state';
import { GFM } from '@lezer/markdown';
import * as cells from './widget-cells';
import { cellAtPosition, cellToOpen } from './widget-cell-state';

vi.mock('./widget-cells', async (original) => {
	const real = await original<typeof import('./widget-cells')>();
	return { ...real, tableCells: vi.fn(real.tableCells), tableGrid: vi.fn(real.tableGrid) };
});

const HEAD = '| Feature | Status | Note |\n| --- | --- | --- |';

describe('cellToOpen', () => {
	it('parses the table block it opens a cell of, never the rest of the document', () => {
		const doc = `${HEAD}\n| Login | Done |\n\n| later | line | here |\n| more | prose | now |\n`;
		const state = EditorState.create({ doc, extensions: [markdown({ extensions: GFM })] });
		vi.mocked(cells.tableCells).mockClear();
		vi.mocked(cells.tableGrid).mockClear();
		expect(cellToOpen(state, 0, 1, 2), 'a padded cell of the table opens').toEqual(expect.objectContaining({ row: 1, col: 2, padded: true }));
		expect(cellToOpen(state, 0, 2, 0), 'the line after the table is not a row of it').toBeNull();
		const parsed = [...vi.mocked(cells.tableCells).mock.calls, ...vi.mocked(cells.tableGrid).mock.calls].map(([src]) => src);
		expect(parsed.length, 'setup: the parser ran').toBeGreaterThan(0);
		expect(parsed.filter((src) => src.includes('later')), 'no parse took in the prose after the table').toEqual([]);
	});
});

describe('cellAtPosition', () => {
	const doc = `Intro\n\n${HEAD}\n| Login | Done | ok |\n\nAfter\n`;
	const state = EditorState.create({ doc, extensions: [markdown({ extensions: GFM })] });

	it('answers the cell whose source holds the position', () => {
		const at = doc.indexOf('Done') + 2;
		const cell = cellAtPosition(state, at)!;
		expect(cell, 'the cell holding the caret').toEqual(expect.objectContaining({ row: 1, col: 1, padded: false }));
		expect(cell.from <= at && at <= cell.to).toBe(true);
		expect(state.sliceDoc(cell.from, cell.to).trim()).toBe('Done');
	});

	it('answers no cell for prose', () => {
		expect(cellAtPosition(state, doc.indexOf('Intro') + 1)).toBeNull();
		expect(cellAtPosition(state, doc.indexOf('After') + 1)).toBeNull();
	});
});
