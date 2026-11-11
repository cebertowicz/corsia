import { afterEach, describe, expect, it } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { build, cellEl, cleanup, DOC, table } from './widget-cell-rig';

/**
 * The drawn table follows its source in place (`widget-table.ts`), and a column's alignment lives only in the delimiter
 * row, which holds no cell's text: a change to it alone has to reach the cells that were left where they were.
 */

const rigs: Rig[] = [];
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; });

const alignOf = (cell: HTMLTableCellElement) => cell.getAttribute('align') ?? cell.style.textAlign ?? '';

describe('a change that only moves a column alignment', () => {
	it('is drawn on the header and body cells of that column, in the same table', () => {
		const r = build(DOC);
		rigs.push(r);
		const drawn = table(r)!, body = cellEl(r, 1, 1);
		expect(alignOf(cellEl(r, 0, 1)), 'setup: nothing is aligned').toBe('');
		const at = DOC.indexOf('| --- | --- |') + '| --- | '.length;
		r.view.dispatch({ changes: { from: at, to: at + 3, insert: ':---:' } });
		expect(r.view.state.doc.toString()).toContain('| --- | :---: |');
		expect(table(r), 'the table was patched, not rebuilt').toBe(drawn);
		expect(alignOf(cellEl(r, 0, 1)), 'the header cell is centred').toBe('center');
		expect(alignOf(cellEl(r, 1, 1)), 'and so is the body cell under it').toBe('center');
		expect(cellEl(r, 1, 1), 'the cell itself was kept').toBe(body);
		expect(alignOf(cellEl(r, 1, 0)), 'the other column is untouched').toBe('');
	});
});
