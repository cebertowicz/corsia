import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import type { Rig } from './typed-reveal-rig';
import { emptyCellSelection } from './widget-cell-comment';
import { openCellState } from './widget-cell-state';
import { intersectsCell, tableCells, tableGrid, widgetCommentLabel } from './widget-cells';
import { build, cellText, cleanup, clicked, table, typed } from './widget-cell-rig';

/**
 * A row shorter than its header is drawn with empty cells padded in at its end, none of which has source of its own. Each
 * one opens in its own column and takes what is typed there, gaining the delimiters of every padded cell before it; none
 * takes a comment until it has text, since every padded cell of the row sits at the same offset (the row's end).
 */

const rigs: Rig[] = [];
const track = (r: Rig) => { rigs.push(r); return r; };
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

const HEAD = '| Feature | Status | Note | Owner |\n| --- | --- | --- | --- |';
/** A four-column table whose body row has only its first `cells` cells in the source. */
const short = (cells: string) => `# Plan\n\n${HEAD}\n${cells}\n\nafter\n`;
const opened = (r: Rig) => r.view.state.field(openCellState, false);
const bodyRow = (r: Rig) => r.view.state.doc.toString().split('\n')[4];

describe('the padded cells of a short row', () => {
	for (const [missing, row] of [[2, '| Login | Done |'], [3, '| Login |']] as const) {
		it(`each open in their own column and take the text typed there, in a row missing ${missing} cells`, () => {
			const real = 4 - missing;
			for (let col = real; col < 4; col++) {
				const r = track(build(short(row)));
				expect(table(r)!.rows[1].cells, 'setup: the row is drawn to the header\'s width').toHaveLength(4);
				clicked(r, table(r)!.rows[1].cells[col]);
				expect(opened(r)?.col, `the editor opens over the clicked cell, column ${col}`).toBe(col);
				expect(cellText(r)).toBe('');
				typed(r, 0, 'Z');
				const expected = `${row}${' |'.repeat(col - real)} Z |`;
				expect(bodyRow(r), `the text lands in column ${col}, with the cells before it delimited empty`).toBe(expected);
				expect(cellText(r)).toBe(' Z ');
				expect(opened(r)?.col, 'the editor stays over that cell').toBe(col);
				expect([...table(r)!.rows[1].cells].map((cell) => cell.textContent), 'drawn in that column').toEqual(
					[...row.split('|').slice(1, -1).map((text) => text.trim()), ...Array(4 - real).fill('')].map((text, at) => at === col ? 'Z' : text));
			}
		});

		it(`have no source for a comment to anchor on, in a row missing ${missing} cells`, () => {
			const src = `${HEAD}\n${row}`, end = src.length;
			const padded = tableGrid(src).filter((span) => span.row === 1 && span.col >= 4 - missing);
			expect(tableCells(src).filter((span) => span.row === 1), 'tableCells has none of them').toHaveLength(4 - missing);
			expect(padded.map((span) => [span.col, span.padded]), 'one span per drawn padded cell').toEqual(
				Array.from({ length: missing }, (_, at) => [4 - missing + at, true]));
			expect(padded.some((span) => intersectsCell(span, end - 1, end) || intersectsCell(span, end, end) || intersectsCell(span, 0, end)),
				'no source range touches any of them').toBe(false);
			const state = EditorState.create({ doc: short(row), extensions: [markdown({ extensions: GFM })] });
			const at = short(row).indexOf(row) + row.length;
			expect(widgetCommentLabel(state, at - 1, at, '|') ?? '', 'the row\'s last pipe names no padded cell').not.toMatch(/Empty cell/);
		});

		it(`offer no Comment while one is open and empty, in a row missing ${missing} cells`, () => {
			const r = track(build(short(row)));
			clicked(r, table(r)!.rows[1].cells[3]);
			expect(opened(r)?.col, 'setup: a padded cell is open').toBe(3);
			expect(emptyCellSelection(r.view)).toBeNull();
		});
	}

	it('leaves a real empty cell its whole-cell Comment offer', () => {
		const r = track(build(short('| Login |  | first | me |')));
		clicked(r, table(r)!.rows[1].cells[1]);
		expect(emptyCellSelection(r.view), 'a cell the source delimits is offered whole').toEqual(expect.objectContaining({ inWidget: true }));
	});
});
