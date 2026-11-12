import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Lexer, type Tokens } from 'marked';
import type { Rig } from './typed-reveal-rig';
import { build, cellEl, cleanup, clicked, MOD, mountedEditor, press, THREEROW } from './widget-cell-rig';

/**
 * Typing below a table that ends the document. Leaving the table puts the caret at the document's end; with no blank line
 * there, GFM (and the editor's own parse) reads the next line as another row, so what is typed there would join the table
 * as a row, or past the header's width as a cell no one sees. The text goes below a blank line instead, at the moment it
 * is typed, so leaving the table alone never changes the document.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

const tables = (text: string) => Lexer.lex(text).filter((t): t is Tokens.Table => t.type === 'table');
const paragraphs = (text: string) => Lexer.lex(text).filter((t): t is Tokens.Paragraph => t.type === 'paragraph').map((t) => t.text);
const ROWS = tables(THREEROW)[0].rows.length;

/** A document whose table is the last thing in it, left by Escape from the last row. */
const leftTable = (doc: string) => {
	const r = track(build(doc));
	clicked(r, cellEl(r, 2, 1));
	press(r, 'Escape');
	expect(mountedEditor(r), 'no cell is open').toBeNull();
	return r;
};
const type = (r: Rig, insert: string, userEvent = 'input.type') => {
	const at = r.view.state.selection.main.head;
	r.view.dispatch({ changes: { from: at, insert }, selection: { anchor: at + insert.length }, userEvent });
};

describe('typing after a table that ends the document', () => {
	it('leaving the table changes nothing in the document', () => {
		const r = leftTable(THREEROW);
		expect(r.view.state.doc.toString()).toBe(THREEROW);
		expect(r.view.state.selection.main.head).toBe(THREEROW.length);
	});

	it('with no newline at the end, what is typed is a paragraph below a blank line, and the table keeps its rows', () => {
		const r = leftTable(THREEROW);
		type(r, 'x');
		const text = r.view.state.doc.toString();
		expect(text).toBe(`${THREEROW}\n\nx`);
		expect(tables(text)[0].rows.length, 'the table keeps its rows').toBe(ROWS);
		expect(paragraphs(text)).toEqual(['x']);
		expect(r.view.state.selection.main.head, 'the caret follows the typed text').toBe(text.length);
		type(r, 'y');
		expect(r.view.state.doc.toString(), 'typing on adds no second blank line').toBe(`${THREEROW}\n\nxy`);
	});

	it('with a newline at the end, one more newline makes the blank line GFM needs', () => {
		const doc = `${THREEROW}\n`;
		expect(tables(`${doc}x`)[0].rows.length, 'GFM reads a line right under a row as another row').toBe(ROWS + 1);
		const r = leftTable(doc);
		expect(r.view.state.selection.main.head).toBe(doc.length);
		type(r, 'x');
		expect(r.view.state.doc.toString()).toBe(`${doc}\nx`);
		expect(paragraphs(r.view.state.doc.toString())).toEqual(['x']);
	});

	it('with a blank line already at the end, adds nothing', () => {
		const doc = `${THREEROW}\n\n`;
		const r = track(build(doc));
		r.view.dispatch({ selection: { anchor: doc.length } });
		type(r, 'x');
		expect(r.view.state.doc.toString()).toBe(`${doc}x`);
	});

	it('a paste and a composed (IME) commit go below the blank line too', () => {
		const r = leftTable(THREEROW);
		type(r, 'pasted | text', 'input.paste');
		expect(r.view.state.doc.toString()).toBe(`${THREEROW}\n\npasted | text`);
		const s = leftTable(THREEROW);
		type(s, '字', 'input.type.compose');
		expect(s.view.state.doc.toString()).toBe(`${THREEROW}\n\n字`);
	});

	it('the typed text and the blank line are one undo step', () => {
		const r = leftTable(THREEROW);
		type(r, 'x');
		r.view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 'z', [MOD]: true, bubbles: true, cancelable: true }));
		expect(r.view.state.doc.toString()).toBe(THREEROW);
	});

	it('leaves an edit that is not typing alone: an agent\'s or a remote edit at the end, and Enter on its own', () => {
		const r = leftTable(THREEROW);
		r.view.dispatch({ changes: { from: THREEROW.length, insert: 'z' } });
		expect(r.view.state.doc.toString(), 'not a reader typing').toBe(`${THREEROW}z`);
		const s = leftTable(THREEROW);
		type(s, '\n');
		expect(s.view.state.doc.toString(), 'a newline alone joins nothing to the table').toBe(`${THREEROW}\n`);
	});

	it('for random typed text at the end of random documents ending in a table, the table keeps its rows and the text is outside it', () => {
		const pieces = ['x', 'ab', ' ', '|', '| a |', '\\', '-', '# h', '\n', '\n\n', 'é', '`c`'];
		const ends = ['', '\n'];
		let state = 7;
		const next = (n: number) => { state = (Math.imul(state, 1103515245) + 12345) >>> 0; return state % n; };
		// One editor per ending, set back to it before each case: mounting an editor per case costs more than the cases.
		const editors = ends.map((end) => track(build(`${THREEROW}${end}`)));
		for (let seed = 0; seed < 300; seed++) {
			const which = next(ends.length), end = ends[which], r = editors[which];
			const base = `${THREEROW}${end}`;
			r.view.dispatch({ changes: { from: 0, to: r.view.state.doc.length, insert: base }, selection: { anchor: base.length } });
			const typedText: string[] = [];
			for (let i = 0, n = 1 + next(4); i < n; i++) {
				const piece = pieces[next(pieces.length)];
				typedText.push(piece);
				type(r, piece, next(2) ? 'input.type' : 'input.paste');
			}
			const text = r.view.state.doc.toString();
			const name = `seed ${seed}: ${JSON.stringify(end)} then ${JSON.stringify(typedText)} gives ${JSON.stringify(text)}`;
			expect(text.startsWith(THREEROW), name).toBe(true);
			expect(tables(text)[0]?.rows.length, name).toBe(ROWS);
			expect(text.slice(THREEROW.length).replace(/\n/g, ''), `${name}: nothing typed is lost or added`).toBe(typedText.join('').replace(/\n/g, ''));
		}
	});
});
