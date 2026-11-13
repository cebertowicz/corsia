import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { openCellAt, openCellState } from './widget-cell-state';
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
		typed(r, 4, '!');
		expect(cellText(r)).toBe('Done!');
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
		typed(r, 4, '!');
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
		typed(r, 5, '!');
		press(r, 'Tab');
		expect(editingCell(r), 'the editor moved on to the next cell').toBe(cellEl(r, 1, 1));
		expect(cellEl(r, 1, 0).textContent, 'the cell behind shows what was typed, unseen under nothing').toBe('Login!');
		expect(r.view.state.doc.toString()).toContain('| Login! | Done | first |');
	});
});

describe('the next cell holds its own text, never the one it came from', () => {
	// A space typed at a cell's edge stays while typing goes on in that cell; it is the reader's, and that cell's only.
	const twin = (word: string) => `| H1 | H2 |\n| --- | --- |\n| ${word} | ${word} |\n| z | ${word} |`;

	it("Tab from 'Done ' to a cell holding 'Done' leaves no space before what is typed next", () => {
		const r = track(build(twin('Done')));
		clicked(r, cellEl(r, 1, 0));
		typed(r, 4, ' ');
		expect(cellText(r), 'the space typed stays in its own cell').toBe('Done ');
		press(r, 'Tab');
		expect(openIn(r, 1, 1)).toBe(true);
		expect(cellText(r), 'the next cell holds what it stores').toBe('Done');
		typed(r, cellCaret(r), 'x');
		expect(r.view.state.doc.toString()).toContain('| Donex |');
	});

	// Every combination once: 4 texts, 3 leading and 2 trailing runs of spaces, and both keys. Each builds its own editor,
	// which takes over 100ms on a loaded machine, so the 48 of them get more than the default 5s.
	it('for any text and edge spaces, after Tab or Enter', { timeout: 30_000 }, () => {
		const cases = ['Done', 'a b', 'x', 'ok `c`'].flatMap((word) => [0, 1, 2].flatMap((leading) => [1, 2].flatMap((trailing) =>
			[false, true].map((viaEnter) => ({ word, lead: ' '.repeat(leading), trail: ' '.repeat(trailing), viaEnter })))));
		for (const { word, lead, trail, viaEnter } of cases) {
			const r = track(build(twin(word)));
			const [row, col] = viaEnter ? [1, 1] : [1, 0];
			clicked(r, cellEl(r, row, col));
			typed(r, cellText(r).length, trail);
			typed(r, 0, lead);
			const name = `'${lead}${word}${trail}' then ${viaEnter ? 'Enter' : 'Tab'}`;
			expect(cellText(r), name).toBe(`${lead}${word}${trail}`);
			press(r, viaEnter ? 'Enter' : 'Tab');
			expect(openIn(r, viaEnter ? 2 : 1, 1), name).toBe(true);
			expect(cellText(r), `${name}: the next cell holds what it stores`).toBe(word);
			typed(r, cellCaret(r), 'x');
			expect(r.view.state.doc.toString(), `${name}: what is typed lands right after its text`).toContain(`| ${word}x |`);
		}
	});
});

describe('a row with more cells than the header', () => {
	// The renderer draws no cell for the third cell of the first body row, so the walk has none to stop at.
	const WIDE = '| A | B |\n| --- | --- |\n| 1 | 2 | 3 |\n| 4 | 5 |';

	it('Tab from its last drawn cell goes to the next row, and Shift-Tab comes back to that cell', () => {
		const r = track(build(WIDE));
		clicked(r, cellEl(r, 1, 1));
		press(r, 'Tab');
		expect(openIn(r, 2, 0), 'the next row first drawn cell').toBe(true);
		press(r, 'Tab', true);
		expect(openIn(r, 1, 1), 'back on the last drawn cell, past the undrawn one').toBe(true);
		expect(cellText(r)).toBe('2');
	});

	it('leaves the reader a caret in an open cell: nothing opens on a cell that is not drawn', () => {
		const r = track(build(WIDE));
		clicked(r, cellEl(r, 1, 1));
		press(r, 'Tab');
		expect(mountedEditor(r), 'an editor is open').not.toBeNull();
		expect(r.view.state.field(openCellState)?.row, 'and the open cell is the drawn one').toBe(2);
		expect(r.view.dom.classList.contains('cm-md-cell-open')).toBe(true);
	});

	it('closes the cell, and the document caret shows, when the cell it was moved to is not drawn', () => {
		const r = track(build(WIDE));
		clicked(r, cellEl(r, 1, 1));
		const start = r.view.state.doc.toString().indexOf('| 3 |') + 1;
		r.view.dispatch({ effects: openCellAt.of({ ...r.view.state.field(openCellState)!, col: 2, from: start, to: start + 3 }) });
		expect(r.view.state.field(openCellState), 'no cell is left open').toBeNull();
		expect(mountedEditor(r)).toBeNull();
		expect(r.view.dom.classList.contains('cm-md-cell-open'), 'so nothing hides the document caret').toBe(false);
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
		typed(r, 5, '!');
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
		caretAt(r, 0);
		press(r, 'ArrowRight');
		press(r, 'ArrowRight');
		expect(cellCaret(r), 'the arrows moved it two characters').toBe(2);
		press(r, 'ArrowRight', true);
		const { from, to } = cellEditor(r)!.state.selection.main;
		expect([from, to], 'and a shifted arrow selects from the caret, which is how a word gets commented').toEqual([2, 3]);
		// Home and End need a laid-out line, which jsdom does not provide; the browser spec presses them in a real cell.
	});

	it('holds the cell as it is drawn, so the caret at either end of it is at the drawn text\'s ends', () => {
		const r = openTable();
		expect(cellText(r), 'no pads to step into').toBe('Login');
		caretAt(r, 0);
		typed(r, 0, 'A');
		caretAt(r, cellText(r).length);
		typed(r, cellText(r).length, 'Z');
		expect(r.view.state.doc.toString(), 'text typed at either end lands beside the drawn text, inside the pads').toContain('| ALoginZ | Done | first |');
	});
});
