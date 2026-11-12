import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { build, caretAt, cellCaret, cellText, cleanup, clicked, table, typed } from './widget-cell-rig';
import { cellToOpen, openCellState } from './widget-cell-state';

/**
 * GFM lets a row leave out its leading or trailing pipe, and the cell at that edge is a real cell of the source, not one the
 * renderer pads in. Whether a cell is padded is the one answer `tableGrid` gives (its span's `padded` flag); text typed at the
 * start of such a cell goes into that cell and moves no other. Writing an edge cell gives the row the pipe on that side, so
 * a cell left blank or starting with block syntax neither drops out of the row nor ends the table (`writeCellEdit`).
 */

const rigs: Rig[] = [];
const track = (r: Rig) => { rigs.push(r); return r; };
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

const HEAD = '| Feature | Status |\n| --- | --- |';
const doc = (row: string) => `# Plan\n\n${HEAD}\n${row}\n\nafter\n`;
const bodyRow = (r: Rig) => r.view.state.doc.toString().split('\n')[4];

describe('a cell at a row edge that has no pipe', () => {
	for (const [row, col, typedRow, cell] of [['Login | a |', 0, '|ZLogin | a |', 'ZLogin '], ['| Login |a', 1, '| Login |Za|', 'Za']] as const) {
		it(`is not padded, and a character typed at its start stays in it, in "${row}"`, () => {
			const r = track(build(doc(row)));
			expect(table(r)!.rows[1].cells, 'setup: the row is drawn with both of its cells').toHaveLength(2);
			expect(cellToOpen(r.view.state, '# Plan\n\n'.length, 1, col)?.padded, 'tableGrid says the cell is the source\'s own').toBe(false);
			clicked(r, table(r)!.rows[1].cells[col]);
			expect(r.view.state.field(openCellState, false)?.padded, 'the open cell is not padded').toBe(false);
			caretAt(r, 0);
			expect(cellCaret(r), 'the caret is at the start of the cell, as after Home').toBe(0);
			typed(r, 0, 'Z');
			expect(bodyRow(r), 'the row keeps its cells in order, and gains the pipe its written edge cell needs').toBe(typedRow);
			expect(table(r)!.rows[1].cells, 'the row still has two cells').toHaveLength(2);
			expect(cellText(r), 'the editor holds the cell the character went into').toBe(cell);
		});
	}
});
