import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { build, caretAt, cellCaret, cellEditor, cellEl, cellText, cleanup, clicked, editingCell, mountedEditor, press, THREEROW, typed } from './widget-cell-rig';

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
const openIn = (r: Rig, row: number, col: number) => !!mountedEditor(r) && editingCell(r) === cellEl(r, row, col);

describe('Tab and Enter move through the table', () => {
	it('Tab opens the next cell, and what is typed there lands there', () => {
		const r = openTable();
		expect(openIn(r, 1, 0)).toBe(true);
		const editor = mountedEditor(r);
		press(r, 'Tab');
		expect(openIn(r, 1, 1), 'the cell to the right is open now').toBe(true);
		expect(mountedEditor(r), 'the same editor travelled with the reader, over the new cell').toBe(editor);
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
		expect(editingCell(r), 'the editor moved on to the next cell').toBe(cellEl(r, 1, 1));
		expect(cellEl(r, 1, 0).textContent, 'the cell behind shows what was typed, unseen under nothing').toBe('Login!');
		expect(r.view.state.doc.toString()).toContain('| Login! | Done | first |');
	});
});

describe('the open cell is named for its place in the table', () => {
	const label = (r: Rig) => cellEditor(r)!.contentDOM.getAttribute('aria-label');

	it('by its column header and body row, at open and after Tab and Enter', () => {
		const r = openTable();
		expect(label(r)).toBe('Feature, row 1');
		press(r, 'Tab');
		expect(label(r)).toBe('Status, row 1');
		press(r, 'Enter');
		expect(label(r)).toBe('Status, row 2');
	});

	it('by its column number when the header is empty, and as the header when it is one', () => {
		const r = track(build('|  | Status |\n| --- | --- |\n| Login | Done |'));
		clicked(r, cellEl(r, 1, 0));
		expect(label(r)).toBe('Column 1, row 1');
		clicked(r, cellEl(r, 0, 1));
		expect(label(r)).toBe('Column 2 header');
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

describe('the ordinary caret keys work in a cell', () => {
	it('arrows move and select as they do in prose', () => {
		const r = openTable();
		caretAt(r, 1);
		press(r, 'ArrowRight');
		press(r, 'ArrowRight');
		expect(cellCaret(r), 'the arrows moved it two characters').toBe(3);
		press(r, 'ArrowRight', true);
		const { from, to } = cellEditor(r)!.state.selection.main;
		expect([from, to], 'and a shifted arrow selects from the caret, which is how a word gets commented').toEqual([3, 4]);
		// Home and End need a laid-out line, which jsdom does not provide; the browser spec presses them in a real cell.
	});

	it('keeps the caret on the drawn text, never in the spaces padding it from its pipes', () => {
		const r = openTable();
		const text = cellText(r);
		const first = text.length - text.trimStart().length, last = text.trimEnd().length;
		expect(first, 'the cell is padded on both sides').toBeGreaterThan(0);
		caretAt(r, 0);
		expect(cellCaret(r), 'a caret before the text goes to its first character').toBe(first);
		caretAt(r, text.length);
		expect(cellCaret(r), 'a caret after the text goes to its last').toBe(last);
		press(r, 'ArrowRight');
		expect(cellCaret(r), 'and an arrow does not take it into the padding').toBe(last);
		press(r, 'ArrowLeft', true);
		const { anchor, head } = cellEditor(r)!.state.selection.main;
		expect([anchor, head], 'a selection is kept to the text the same way').toEqual([last, last - 1]);
	});
});
