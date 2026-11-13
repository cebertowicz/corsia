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

/**
 * The neighbours of an alignment-only edit: whatever the edit, the patched table must be the table a fresh render of the
 * final source would draw. Each case edits a built table and compares the drawn table's HTML to one drawn from scratch.
 */
const fresh = (doc: string) => {
	const r = build(doc);
	rigs.push(r);
	return table(r)?.outerHTML ?? null;
};
type Change = { from: number; to: number; insert: string };
const edited = (doc: string, changes: Change[]) => {
	const r = build(doc);
	rigs.push(r);
	r.view.dispatch({ changes });
	return { r, drawn: table(r)?.outerHTML ?? null, doc: r.view.state.doc.toString() };
};
const swap = (doc: string, was: string, now: string): Change => ({ from: doc.indexOf(was), to: doc.indexOf(was) + was.length, insert: now });

describe('the patched table equals a fresh render', () => {
	const WIDE = '| Feature | Status | Note |\n| --- | --- | --- |\n| Login | Done | first |\n| Sync | Open | last |';

	it('when a column changes alignment and its header and body text change in the same edit', () => {
		const out = edited(WIDE, [swap(WIDE, '| --- | --- | --- |', '| --- | :---: | --- |'), swap(WIDE, 'Status', 'State'), swap(WIDE, 'Done', 'Shipped')]);
		expect(out.drawn).toBe(fresh(out.doc));
		expect(alignOf(table(out.r)!.rows[0].cells[1]), 'the changed header is centred').toBe('center');
		expect(alignOf(table(out.r)!.rows[2].cells[1]), 'and so is the unchanged cell under it').toBe('center');
	});

	it('when alignment moves between columns', () => {
		const aligned = WIDE.replace('| --- | --- | --- |', '| :--- | --- | --- |');
		const out = edited(aligned, [swap(aligned, '| :--- | --- | --- |', '| --- | --- | ---: |')]);
		expect(out.drawn).toBe(fresh(out.doc));
		expect(alignOf(table(out.r)!.rows[1].cells[0]), 'the column that lost its alignment shows none').toBe('');
	});

	it('when the delimiter row loses a column, which ends the table', () => {
		const out = edited(WIDE, [swap(WIDE, '| --- | --- | --- |', '| --- | --- |')]);
		expect(out.drawn).toBe(fresh(out.doc));
	});

	it('when the delimiter row gains a column the header has not', () => {
		const out = edited(WIDE, [swap(WIDE, '| --- | --- | --- |', '| --- | --- | --- | --- |')]);
		expect(out.drawn).toBe(fresh(out.doc));
	});

	it('when the table has no body rows', () => {
		const head = '| Feature | Status |\n| --- | --- |';
		const out = edited(head, [swap(head, '| --- | --- |', '| ---: | :---: |')]);
		expect(out.drawn).toBe(fresh(out.doc));
		expect(alignOf(table(out.r)!.rows[0].cells[0]), 'the header cell is right-aligned').toBe('right');
	});

	it('when a body row is shorter or longer than the header', () => {
		const ragged = '| A | B | C |\n| --- | --- | --- |\n| 1 |\n| 1 | 2 | 3 | 4 |\n| x | y | z |';
		const out = edited(ragged, [swap(ragged, '| --- | --- | --- |', '| --- | :---: | ---: |')]);
		expect(out.drawn).toBe(fresh(out.doc));
		expect(alignOf(table(out.r)!.rows[3].cells[2]), 'the last body row follows the same columns').toBe('right');
	});
});
