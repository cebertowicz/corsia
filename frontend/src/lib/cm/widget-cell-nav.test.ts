import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { build, cellEditor, cellEl, cellText, cleanup, clicked, mountedEditor, press, THREEROW, typed } from './widget-cell-rig';

/**
 * Moving around a table from inside a cell: Tab and Shift-Tab walk the cells and wrap at the row ends, Enter goes down a
 * column, and leaving the table from its last cell or row puts the caret below it. The cells are the rendered table's, so
 * the movement the reader sees is the movement they get.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

const openTable = () => {
	const r = track(build(THREEROW));
	clicked(r, cellEl(r, 1, 0));
	return r;
};
const openIn = (r: Rig, row: number, col: number) => !!cellEl(r, row, col).querySelector('.cm-md-cell-editor');

describe('Tab and Enter move through the table', () => {
	it('Tab opens the next cell, and what is typed there lands there', () => {
		const r = openTable();
		expect(openIn(r, 1, 0)).toBe(true);
		press(r, 'Tab');
		expect(openIn(r, 1, 1), 'the cell to the right is open now').toBe(true);
		expect(mountedEditor(r) === cellEl(r, 1, 1).querySelector('.cm-md-cell-editor')).toBe(true);
		typed(r, 5, '!');
		expect(cellText(r)).toBe(' Done! ');
		expect(r.view.state.doc.toString()).toContain('| Login | Done! | first |');
	});

	it('Tab at the end of a row wraps to the next row', () => {
		const r = openTable();
		clicked(r, cellEl(r, 1, 2));
		press(r, 'Tab');
		expect(openIn(r, 2, 0), 'the first cell of the next row').toBe(true);
	});

	it('Shift-Tab goes back to the previous cell', () => {
		const r = openTable();
		clicked(r, cellEl(r, 1, 1));
		press(r, 'Tab', true);
		expect(openIn(r, 1, 0)).toBe(true);
	});

	it('Tab from the last cell leaves the table with the caret below it', () => {
		const r = openTable();
		clicked(r, cellEl(r, 2, 2));
		press(r, 'Tab');
		expect(mountedEditor(r), 'no cell is open').toBeNull();
		expect(r.view.state.selection.main.head, 'the caret is after the table').toBe(r.view.state.doc.length);
		expect(r.view.state.doc.toString()).toBe(THREEROW);
	});

	it('Enter goes to the same column of the next row', () => {
		const r = openTable();
		clicked(r, cellEl(r, 1, 1));
		press(r, 'Enter');
		expect(openIn(r, 2, 1)).toBe(true);
		typed(r, 5, '!');
		expect(r.view.state.doc.toString()).toContain('| Sync | Open! | last |');
	});

	it('Enter from the last row leaves the table', () => {
		const r = openTable();
		clicked(r, cellEl(r, 2, 1));
		press(r, 'Enter');
		expect(mountedEditor(r)).toBeNull();
		expect(r.view.state.selection.main.head).toBe(r.view.state.doc.length);
	});

	it('the cell that was left shows its text again, rendered', () => {
		const r = openTable();
		typed(r, 6, '!');
		press(r, 'Tab');
		expect(mountedEditor(r) === cellEl(r, 1, 1).querySelector('.cm-md-cell-editor')).toBe(true);
		expect(cellEl(r, 1, 0).textContent, 'the cell behind shows what was typed, without the editor').toBe('Login!');
		expect(r.view.state.doc.toString()).toContain('| Login! | Done | first |');
	});
});

describe('Escape leaves the table', () => {
	it('closes the cell and puts the caret after the table', () => {
		const r = openTable();
		typed(r, 6, '!');
		press(r, 'Escape');
		expect(mountedEditor(r)).toBeNull();
		expect(cellEl(r, 1, 0).textContent, 'the cell is rendered text again').toBe('Login!');
		expect(r.view.state.selection.main.head).toBe(r.view.state.doc.length);
		expect(cellEditor(r)).toBeNull();
	});
});
