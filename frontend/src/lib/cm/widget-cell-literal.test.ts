import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { openCellState } from './widget-cell-state';
import { build, cellEditor, cellEl, cellText, cleanup, clicked, mountedEditor, press } from './widget-cell-rig';

/**
 * A cell is inline content: markdown's block syntax does not start a list, heading, rule or quote inside one, so the table
 * draws it as the literal text. The open cell's editor parses only inline syntax, so it shows the reader that same text,
 * and a key that block syntax would take (Enter continuing a list or quote) is the table's key, not a new block.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

const doc = (cell: string) => `# Plan\n\n| Feature | Status |\n| --- | --- |\n| Login | ${cell} |\n| Sync | Open |\n\nafter\n`;

describe('a cell holding text that is block syntax outside a table', () => {
	for (const cell of ['-', '- foo', '# x', '---', '> q', '1. x']) {
		it(`opens showing ${JSON.stringify(cell)} as the table draws it, and Enter moves down its column leaving the document as it was`, () => {
			const source = doc(cell);
			const r = track(build(source));
			const drawn = cellEl(r, 1, 1).textContent;
			expect(drawn, 'setup: the closed table draws the literal text').toBe(cell);
			clicked(r, cellEl(r, 1, 1));
			expect(cellText(r), 'the editor holds the cell as it is drawn').toBe(cell);
			expect(cellEditor(r)!.contentDOM.textContent, 'and shows it as drawn').toBe(drawn);
			cellEditor(r)!.dispatch({ selection: { anchor: cell.length } });
			press(r, 'Enter');
			expect(mountedEditor(r), 'a cell is still open').not.toBeNull();
			expect(r.view.state.field(openCellState), 'the same column of the next row').toMatchObject({ row: 2, col: 1 });
			expect(cellText(r)).toBe('Open');
			expect(r.view.state.doc.toString(), 'the document is unchanged').toBe(source);
			expect(cellEl(r, 1, 1).textContent, 'the cell left behind draws the same text').toBe(cell);
		});
	}
});
