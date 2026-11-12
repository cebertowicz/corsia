import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { tableCells } from './widget-cells';
import { readCell, writeCellEdit } from './widget-cell-row';
import {
	build, caretAt, cellCaret, cellEditor, cellText, cleanup, clicked, deleted, DOC, open, pasted, press, table, typed
} from './widget-cell-rig';

/**
 * A cell's source sits between two pipes, so every `|` inside it has to stay escaped (an odd run of backslashes before
 * it) or the row splits into more cells than it had. The cell's editor holds the cell's content, its source with those
 * table escapes taken out, so the reader edits what is drawn; `writeCellEdit` writes each `|` back escaped, and whatever
 * they type, paste or delete, the saved row keeps its cells and the text lands where they meant it.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

/** A body row whose first cell already holds an escaped pipe: ` a\|b ` between its delimiters. */
const PIPED_ROW = '| a\\|b | Done |';
const PIPED = DOC.replace('| Login | Done |', PIPED_ROW);

/** The body row's cells as the repo's own table scan splits the saved source. */
function savedRow(r: Rig) {
	const lines = r.view.state.doc.toString().split('\n');
	const head = lines.indexOf('| Feature | Status |');
	const src = lines.slice(head, head + 3).join('\n');
	return tableCells(src).filter((span) => span.row === 1).map((span) => src.slice(span.from, span.to));
}

/** Every `|` in the text has an odd run of backslashes right before it. */
function allPipesEscaped(text: string) {
	let run = 0;
	for (const char of text) {
		if (char === '|' && run % 2 === 0) return false;
		run = char === '\\' ? run + 1 : 0;
	}
	return true;
}

describe('nothing typed or pasted breaks the table', () => {
	it('stores a typed pipe escaped, so the row keeps its columns', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, '|no');
		const text = r.view.state.doc.toString();
		expect(text).toContain('Login\\|no');
		expect(text).not.toContain('Login|no');
		expect(table(r)!.rows[1].cells).toHaveLength(2);
		expect(cellText(r), 'the cell editor holds the content, the pipe as typed').toBe(' Login|no ');
		expect(cellCaret(r), 'the caret is after what went in, so the next key lands after it').toBe(9);
		typed(r, cellCaret(r), '!');
		expect(cellText(r)).toBe(' Login|no! ');
	});

	it('keeps a backslash typed a keystroke before a pipe as text in front of the pipe', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, '\\');
		typed(r, 7, '|');
		expect(cellText(r), 'the backslash stays, drawn, in front of the pipe').toBe(' Login\\\\| ');
		expect(savedRow(r), 'the saved row is still two cells, the pipe escaped').toEqual(['Login\\\\\\|', 'Done']);
		expect(table(r)!.rows[1].cells, 'the row is still two columns').toHaveLength(2);
	});

	it('keeps a pasted block of lines inside the cell', () => {
		const r = track(open());
		clicked(r);
		pasted(r, 6, 'two\nlines');
		expect(cellText(r), 'the break became a space where it was pasted').toBe(' Logintwo lines ');
		expect(cellCaret(r), 'the caret is at the end of the paste').toBe(15);
		const text = r.view.state.doc.toString();
		expect(text).toContain('| Logintwo lines | Done |');
		expect(text.split('\n'), 'nothing was added to or taken from the table\'s lines').toHaveLength(DOC.split('\n').length);
	});

	it('has nowhere for a line break to go', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, '\n');
		expect(r.view.state.doc.toString()).toBe(DOC);
		expect(cellText(r)).toBe(' Login ');
	});

	it('deletes an escaped pipe whole, its escape with it', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, '|');
		expect(r.view.state.doc.toString()).toContain('Login\\|');
		deleted(r, 6, 7);
		expect(r.view.state.doc.toString()).toBe(DOC);
		expect(cellCaret(r), 'the caret is where the pipe was').toBe(6);
		expect(table(r)!.rows[1].cells).toHaveLength(2);
	});
});

describe('an escaped pipe the cell already holds stays escaped', () => {
	it('keeps a backslash typed just before the pipe as a literal backslash in front of it', () => {
		const r = track(build(PIPED));
		clicked(r);
		expect(cellText(r), 'setup: the cell holds the pipe, its table escape taken out').toBe(' a|b ');
		typed(r, 2, '\\');
		expect(cellText(r), 'a literal backslash, then the pipe').toBe(' a\\\\|b ');
		expect(savedRow(r), 'the saved row splits into the same two cells').toEqual(['a\\\\\\|b', 'Done']);
		expect(table(r)!.rows[1].cells).toHaveLength(2);
		typed(r, cellCaret(r), 'c');
		expect(cellText(r), 'the next key lands after the backslash, before the pipe').toBe(' a\\\\c|b ');
		expect(savedRow(r)).toEqual(['a\\\\c\\|b', 'Done']);
	});

	it('keeps the pipe escaped when a deletion joins a backslash to it', () => {
		const r = track(build(PIPED.replace('a\\|b', 'a\\x\\|b')));
		clicked(r);
		expect(cellText(r), 'setup').toBe(' a\\x|b ');
		deleted(r, 3, 4);
		expect(cellText(r), 'the remaining backslash is literal, in front of the pipe').toBe(' a\\\\|b ');
		expect(savedRow(r)).toEqual(['a\\\\\\|b', 'Done']);
	});

	it('removes a literal backslash in front of an escaped pipe with Backspace or Delete, the pipe staying a pipe', () => {
		const literal = PIPED.replace('a\\|b', 'a\\\\\\|b');
		for (const [name, from, to] of [['Backspace after it', 3, 4], ['Delete before it', 2, 3]] as const) {
			const r = track(build(literal));
			clicked(r);
			expect(cellText(r), 'setup: a literal backslash, then the pipe').toBe(' a\\\\|b ');
			deleted(r, from, to);
			expect(cellText(r), `${name}: the backslash is gone and the pipe is still a pipe`).toBe(' a|b ');
			expect(cellCaret(r), `${name}: the caret is where the backslash was`).toBe(2);
			expect(savedRow(r)).toEqual(['a\\|b', 'Done']);
		}
	});

	it('removes the drawn backslash with Backspace whichever way the caret got right after it', () => {
		// Drawn `a\|b`: content ` a`, a markdown escape (hidden), the drawn backslash, the pipe. Right after the drawn
		// backslash is content offset 4.
		const literal = PIPED.replace('a\\|b', 'a\\\\\\|b');
		const routes: [string, (r: Rig) => void][] = [
			['a click right after it', (r) => caretAt(r, 4)],
			['End then ArrowLeft', (r) => { press(r, 'End'); press(r, 'ArrowLeft'); press(r, 'ArrowLeft'); }],
			['ArrowRight from the start', (r) => { press(r, 'Home'); for (let i = 0; i < 3; i++) press(r, 'ArrowRight'); }]
		];
		for (const [name, place] of routes) {
			const r = track(build(literal));
			clicked(r);
			expect(cellText(r), 'setup: a literal backslash, then the pipe').toBe(' a\\\\|b ');
			place(r);
			expect(cellCaret(r), `${name}: the caret is right after the drawn backslash`).toBe(4);
			press(r, 'Backspace');
			expect(cellText(r), `${name}: the drawn backslash is gone and the pipe is still a pipe`).toBe(' a|b ');
			expect(savedRow(r), `${name}: the saved row`).toEqual(['a\\|b', 'Done']);
			expect(table(r)!.rows[1].cells).toHaveLength(2);
		}
	});

	it('still removes an escaped pipe whole with Delete in front of it', () => {
		const r = track(build(PIPED));
		clicked(r);
		caretAt(r, 2);
		press(r, 'Delete');
		expect(cellText(r)).toBe(' ab ');
		expect(savedRow(r)).toEqual(['ab', 'Done']);
	});

	const ROW = '| a | Done |';
	/** A content with a pipe, a literal backslash before a pipe, and a trailing backslash. */
	const START = ' a|\\\\|b\\ ';
	const BASE = writeCellEdit(ROW, 0, START).row;
	/** The row with `content` written as its first cell: still two cells, every pipe in the first escaped. */
	function keepsRow(content: string, edited: { from: number; to: number }) {
		const written = writeCellEdit(BASE, 0, content, edited).row;
		const cells = tableCells(written);
		return cells.length === 2 && allPipesEscaped(written.slice(cells[0].rawFrom, cells[0].rawTo)) && readCell(written, 1) === ' Done ';
	}

	it('keeps every pipe escaped whatever is typed at any place in the cell', () => {
		expect(tableCells(BASE), 'setup').toHaveLength(2);
		for (let at = 0; at <= START.length; at++) {
			for (const insert of ['\\', '|', 'x', '\\|', '\\\\', 'x\\']) {
				const content = START.slice(0, at) + insert + START.slice(at);
				expect(keepsRow(content, { from: at, to: at }), `${JSON.stringify(insert)} at ${at}`).toBe(true);
			}
		}
	});

	it('keeps every pipe escaped whatever single character Backspace or Delete removes', () => {
		for (let at = 0; at < START.length; at++) {
			const content = START.slice(0, at) + START.slice(at + 1);
			expect(keepsRow(content, { from: at, to: at + 1 }), `removing ${at}`).toBe(true);
		}
	});
});

describe('a pipe inside inline code in a cell', () => {
	const CODE = PIPED.replace('a\\|b', '`a\\|b`');
	/** What the cell editor draws: a hidden range is not in its DOM. */
	const drawn = (r: Rig) => cellEditor(r)!.contentDOM.textContent;

	it('never shows its backslash, as the closed cell never does', () => {
		const r = track(build(CODE));
		expect(table(r)!.rows[1].cells[0].textContent, 'setup: the closed cell draws the pipe alone').toBe('a|b');
		clicked(r);
		expect(cellText(r), 'the cell holds the code span with the pipe alone').toBe(' `a|b` ');
		expect(drawn(r)).toBe(' a|b ');
	});

	it('shows no backslash for a pipe typed there either', () => {
		const r = track(build(PIPED.replace('a\\|b', '`ab`')));
		clicked(r);
		typed(r, 3, '|');
		expect(cellText(r)).toBe(' `a|b` ');
		expect(drawn(r)).toBe(' a|b ');
		expect(savedRow(r)).toEqual(['`a\\|b`', 'Done']);
	});

	it('keeps the backslash visible in body text, where a code span has no escapes', () => {
		const r = track(build('`a\\|b`\n\n' + DOC));
		expect(r.view.contentDOM.textContent).toContain('a\\|b');
	});

	it('deletes the character before the pipe on Backspace there, the pipe staying escaped', () => {
		const r = track(build(CODE));
		clicked(r);
		caretAt(r, 3);
		press(r, 'Backspace');
		expect(cellText(r), 'the "a" went').toBe(' `|b` ');
		expect(savedRow(r)).toEqual(['`\\|b`', 'Done']);
	});

	it('deletes the pipe, with its escape, on Backspace after it', () => {
		const r = track(build(CODE));
		clicked(r);
		caretAt(r, 4);
		press(r, 'Backspace');
		expect(cellText(r)).toBe(' `ab` ');
		expect(savedRow(r)).toEqual(['`ab`', 'Done']);
	});
});
