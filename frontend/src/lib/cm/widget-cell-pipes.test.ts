import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import type { Rig } from './typed-reveal-rig';
import { tableCells } from './widget-cells';
import { cellShapeOnly } from './widget-cell-text';
import {
	build, caretAt, cellCaret, cellEditor, cellText, cleanup, clicked, deleted, DOC, open, pasted, press, table, typed
} from './widget-cell-rig';

/**
 * A cell's source sits between two pipes, so every `|` inside it has to stay escaped — an odd run of backslashes before
 * it — or the row splits into more cells than it had. The escape's backslash is hidden while the cell is edited, so the
 * reader cannot see which side of it the caret is on; whatever they type, paste or delete, the cell's text keeps every
 * pipe escaped and lands where they meant it.
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
		expect(cellText(r), 'the cell editor holds the escaped source too').toBe(' Login\\|no ');
		expect(cellCaret(r), 'the caret is after what went in, escape included, so the next key lands after it').toBe(10);
		typed(r, cellCaret(r), '!');
		expect(cellText(r)).toBe(' Login\\|no! ');
	});

	it('leaves the pipe escaped when a backslash was typed a keystroke earlier', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, '\\');
		typed(r, 7, '|');
		const text = r.view.state.doc.toString();
		expect(text, 'the earlier backslash already escapes it, so no second one is added').toContain('Login\\|');
		expect(text).not.toContain('Login\\\\|');
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

	it('deletes both halves of an escaped pipe together', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, '|');
		expect(r.view.state.doc.toString()).toContain('Login\\|');
		deleted(r, 6, 7);
		expect(r.view.state.doc.toString()).toBe(DOC);
		expect(cellCaret(r), 'the caret is where the pair was').toBe(6);
		expect(table(r)!.rows[1].cells).toHaveLength(2);
	});
});

describe('an escaped pipe the cell already holds stays escaped', () => {
	it('keeps a backslash typed just before the hidden one as a literal backslash in front of the pipe', () => {
		const r = track(build(PIPED));
		clicked(r);
		expect(cellText(r), 'setup: the cell holds an escaped pipe').toBe(' a\\|b ');
		typed(r, 2, '\\');
		expect(cellText(r), 'a literal backslash, then the escaped pipe').toBe(' a\\\\\\|b ');
		expect(savedRow(r), 'the saved row splits into the same two cells').toEqual(['a\\\\\\|b', 'Done']);
		expect(table(r)!.rows[1].cells).toHaveLength(2);
		typed(r, cellCaret(r), 'c');
		expect(cellText(r), 'the next key lands after the backslash, before the pipe').toBe(' a\\\\c\\|b ');
	});

	it('keeps a backslash typed between the hidden backslash and its pipe as a literal backslash', () => {
		const r = track(build(PIPED));
		clicked(r);
		typed(r, 3, '\\');
		expect(cellText(r)).toBe(' a\\\\\\|b ');
		expect(savedRow(r)).toEqual(['a\\\\\\|b', 'Done']);
	});

	it('puts a character typed between the hidden backslash and its pipe in front of the pair', () => {
		const r = track(build(PIPED));
		clicked(r);
		typed(r, 3, 'x');
		expect(cellText(r), 'the pair stays whole, with the character before it').toBe(' ax\\|b ');
		expect(savedRow(r)).toEqual(['ax\\|b', 'Done']);
		typed(r, cellCaret(r), 'y');
		expect(cellText(r), 'and the next one follows it').toBe(' axy\\|b ');
		expect(savedRow(r)).toEqual(['axy\\|b', 'Done']);
	});

	it('keeps the pipe escaped when a deletion joins a backslash to its escape', () => {
		const r = track(build(PIPED.replace('a\\|b', 'a\\x\\|b')));
		clicked(r);
		deleted(r, 3, 4);
		expect(cellText(r), 'the remaining backslash is literal and the pipe still escaped').toBe(' a\\\\\\|b ');
		expect(savedRow(r)).toEqual(['a\\\\\\|b', 'Done']);
	});

	it('removes a literal backslash in front of an escaped pipe with Backspace or Delete, the pipe staying a pipe', () => {
		const literal = PIPED.replace('a\\|b', 'a\\\\\\|b');
		for (const [name, from, to] of [['Backspace after it', 3, 4], ['Delete before it', 2, 3]] as const) {
			const r = track(build(literal));
			clicked(r);
			expect(cellText(r), 'setup: a literal backslash, then an escaped pipe').toBe(' a\\\\\\|b ');
			deleted(r, from, to);
			expect(cellText(r), `${name}: the backslash is gone and the pipe is still escaped`).toBe(' a\\|b ');
			expect(cellCaret(r), `${name}: the caret is where the backslash was`).toBe(2);
			expect(savedRow(r)).toEqual(['a\\|b', 'Done']);
		}
	});

	it('removes the drawn backslash with Backspace whichever way the caret got right after it', () => {
		// Drawn `a\|b`: source ` a`, a literal backslash (two), the pipe's hidden escape, the pipe. The caret right after the
		// drawn backslash is either before the hidden escape (4) or between it and the pipe (5): the same place on screen.
		const literal = PIPED.replace('a\\|b', 'a\\\\\\|b');
		const routes: [string, (r: Rig) => void][] = [
			['a click between the hidden escape and the pipe', (r) => caretAt(r, 5)],
			['End then ArrowLeft', (r) => { press(r, 'End'); press(r, 'ArrowLeft'); press(r, 'ArrowLeft'); }],
			['ArrowRight from the start', (r) => { press(r, 'Home'); for (let i = 0; i < 3; i++) press(r, 'ArrowRight'); }]
		];
		for (const [name, place] of routes) {
			const r = track(build(literal));
			clicked(r);
			expect(cellText(r), 'setup: a literal backslash, then an escaped pipe').toBe(' a\\\\\\|b ');
			place(r);
			expect([4, 5], `${name}: the caret is right after the drawn backslash`).toContain(cellCaret(r));
			press(r, 'Backspace');
			expect(cellText(r), `${name}: the drawn backslash is gone and the pipe is still escaped`).toBe(' a\\|b ');
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

	it('keeps every pipe escaped whatever is typed at any place in the cell', () => {
		const start = ' a\\|\\\\\\|b\\ ';
		for (let at = 0; at <= start.length; at++) {
			for (const insert of ['\\', '|', 'x', '\\|', '\\\\', 'x\\']) {
				const state = EditorState.create({ doc: start, extensions: EditorState.transactionFilter.of(cellShapeOnly) });
				const text = state.update({ changes: { from: at, insert }, userEvent: 'input.type' }).state.doc.toString();
				expect(allPipesEscaped(text), `${JSON.stringify(insert)} at ${at} gives ${JSON.stringify(text)}`).toBe(true);
			}
		}
	});

	it('keeps every pipe escaped, and takes something out, whatever single character Backspace or Delete removes', () => {
		const start = ' a\\|\\\\\\|b\\\\\\\\\\|c\\ ';
		for (let at = 0; at < start.length; at++) {
			for (const userEvent of ['delete.backward', 'delete.forward']) {
				const state = EditorState.create({ doc: start, extensions: EditorState.transactionFilter.of(cellShapeOnly) });
				const text = state.update({ changes: { from: at, to: at + 1 }, userEvent }).state.doc.toString();
				expect(allPipesEscaped(text) && text !== start, `${userEvent} of ${at} gives ${JSON.stringify(text)}`).toBe(true);
			}
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
		expect(cellText(r), 'setup: the cell holds the escaped pipe in a code span').toBe(' `a\\|b` ');
		expect(drawn(r)).toBe(' a|b ');
	});

	it('shows no backslash for a pipe typed there either', () => {
		const r = track(build(PIPED.replace('a\\|b', '`ab`')));
		clicked(r);
		typed(r, 3, '|');
		expect(cellText(r)).toBe(' `a\\|b` ');
		expect(drawn(r)).toBe(' a|b ');
	});

	it('keeps the backslash visible in body text, where a code span has no escapes', () => {
		const r = track(build('`a\\|b`\n\n' + DOC));
		expect(r.view.contentDOM.textContent).toContain('a\\|b');
	});

	it('treats the caret by the hidden backslash as one before the pipe: Backspace deletes the character drawn before it', () => {
		const r = track(build(CODE));
		clicked(r);
		caretAt(r, 4);
		press(r, 'Backspace');
		expect(cellText(r), 'the "a" went, the pair stayed whole').toBe(' `\\|b` ');
		expect(savedRow(r)).toEqual(['`\\|b`', 'Done']);
	});

	it('deletes the pipe, with its backslash, on Backspace after it', () => {
		const r = track(build(CODE));
		clicked(r);
		caretAt(r, 5);
		press(r, 'Backspace');
		expect(cellText(r)).toBe(' `ab` ');
		expect(savedRow(r)).toEqual(['`ab`', 'Done']);
	});
});
