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

	it('for random typed text at the end of random documents ending in a table, the table keeps its rows and the text is outside it (end of document)', () => {
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

/**
 * Typing on the line directly under a table in the middle of a document. Every way of leaving the table puts the caret on
 * that line; the text typed there is its own paragraph, with a blank line above it and a blank line below it before the
 * block that follows, and the table and that block are unchanged.
 */
describe('typing on the line under a table in the middle of a document', () => {
	const NEXT = 'Next paragraph stays a paragraph.';
	const MID = `# Plan\n\n${THREEROW}\n\n${NEXT}\n`;
	const UNDER = MID.indexOf(THREEROW) + THREEROW.length + 1;
	const WANT = `# Plan\n\n${THREEROW}\n\nx\n\n${NEXT}\n`;
	const blocks = (text: string) => Lexer.lex(text).filter((t) => t.type !== 'space').map((t) => `${t.type}:${t.raw.trim()}`);

	const routes: Array<[string, (r: Rig) => void]> = [
		['Escape', (r) => { clicked(r, cellEl(r, 1, 1)); press(r, 'Escape'); }],
		['Tab from the last cell', (r) => { clicked(r, cellEl(r, 2, 2)); press(r, 'Tab'); }],
		['Enter from the last row', (r) => { clicked(r, cellEl(r, 2, 0)); press(r, 'Enter'); }],
		// A click on the line, or an arrow key onto it, puts the document's caret there with no cell open.
		['a caret placed on the line', (r) => r.view.dispatch({ selection: { anchor: UNDER }, userEvent: 'select.pointer' })],
	];

	for (const [name, leave] of routes) {
		it(`after ${name}, typed text is its own paragraph and the table and the next paragraph are unchanged`, () => {
			const r = track(build(MID));
			leave(r);
			expect(mountedEditor(r), 'no cell is open').toBeNull();
			expect(r.view.state.doc.toString(), 'arriving changes nothing').toBe(MID);
			expect(r.view.state.selection.main.head, 'the caret is on the line under the table').toBe(UNDER);
			type(r, 'x');
			const text = r.view.state.doc.toString();
			expect(text).toBe(WANT);
			expect(tables(text)[0].rows.length, 'the table keeps its rows').toBe(ROWS);
			expect(paragraphs(text)).toEqual(['x', NEXT]);
			expect(r.view.state.selection.main.head, 'the caret follows the typed text').toBe(WANT.indexOf('x\n') + 1);
			type(r, 'y');
			expect(r.view.state.doc.toString(), 'typing on adds nothing more').toBe(WANT.replace('x\n', 'xy\n'));
		});
	}

	it('the typed text and both blank lines are one undo step', () => {
		const r = track(build(MID));
		routes[0][1](r);
		type(r, 'x');
		r.view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 'z', [MOD]: true, bubbles: true, cancelable: true }));
		expect(r.view.state.doc.toString()).toBe(MID);
	});

	it('a paste ending in a line break needs no second break before the next block', () => {
		const r = track(build(MID));
		routes[3][1](r);
		type(r, 'pasted\n', 'input.paste');
		expect(r.view.state.doc.toString()).toBe(WANT.replace('x\n', 'pasted\n'));
	});

	it('for random documents and random typed text on the line under a table, the table and the next block are unchanged and nothing typed is lost', () => {
		const pieces = ['x', 'ab', ' ', '|', '| a |', '\\', 'é', '`c`', '\n'];
		// The last four follow the table with no blank line: each block ends the table on its own.
		const afters = ['', `\n${NEXT}\n`, '\n## Heading\n', '\n- item\n', `\n${THREEROW}\n`, '\n', '## Heading\n', '- item\n', '> quoted\n', '```\ncode\n```\n'];
		const befores = ['', 'Intro.\n\n', '# Plan\n\n'];
		let state = 11;
		const next = (n: number) => { state = (Math.imul(state, 1103515245) + 12345) >>> 0; return state % n; };
		const r = track(build(MID));
		for (let seed = 0; seed < 300; seed++) {
			const base = `${befores[next(befores.length)]}${THREEROW}\n${afters[next(afters.length)]}`;
			const under = base.indexOf(THREEROW) + THREEROW.length + 1;
			r.view.dispatch({ changes: { from: 0, to: r.view.state.doc.length, insert: base }, selection: { anchor: under } });
			const typedText: string[] = [];
			for (let i = 0, n = 1 + next(4); i < n; i++) {
				// The first piece is text typed on the line under the table. An Enter pressed there first moves the reader to a
				// new line that is no longer under the table, where typing joins the next paragraph as it does anywhere in prose.
				const piece = i === 0 ? pieces[next(pieces.length - 1)] : pieces[next(pieces.length)];
				typedText.push(piece);
				type(r, piece, next(2) ? 'input.type' : 'input.paste');
			}
			const text = r.view.state.doc.toString();
			const name = `seed ${seed}: ${JSON.stringify(base)} then ${JSON.stringify(typedText)} gives ${JSON.stringify(text)}`;
			expect(tables(text)[0]?.rows.length, name).toBe(ROWS);
			expect(tables(text).length, `${name}: no table gained or lost`).toBe(tables(base).length);
			const was = blocks(base), now = blocks(text);
			for (const block of was) expect(now, `${name}: ${block} is unchanged`).toContain(block);
			const flat = (s: string) => s.replace(/\n/g, '');
			const typed = typedText.join('');
			expect(flat(text), `${name}: nothing typed is lost or added`).toBe(flat(base.slice(0, under) + typed + base.slice(under)));
		}
	});
});

/**
 * A table can end with no blank line when a heading, list item, blockquote or fence starts on the very next line. Every way
 * of leaving the table puts the caret at the start of that block; text typed there is its own paragraph, with a blank line
 * above it and a blank line before the block, and the table and the block are unchanged.
 */
describe('typing at the start of a block that follows a table with no blank line', () => {
	const BLOCKS: Array<[string, string]> = [
		['a heading', '## Heading'],
		['a list item', '- item'],
		['a blockquote', '> quoted'],
		['a fence', '```\ncode\n```']
	];
	const blocks = (text: string) => Lexer.lex(text).filter((t) => t.type !== 'space').map((t) => `${t.type}:${t.raw.trim()}`);
	const routes: Array<[string, (r: Rig, at: number) => void]> = [
		['Escape', (r) => { clicked(r, cellEl(r, 1, 1)); press(r, 'Escape'); }],
		['Tab from the last cell', (r) => { clicked(r, cellEl(r, 2, 2)); press(r, 'Tab'); }],
		['Enter from the last row', (r) => { clicked(r, cellEl(r, 2, 0)); press(r, 'Enter'); }],
		// A click at the block's start, or an arrow key onto it, puts the document's caret there with no cell open.
		['a caret placed at the block\'s start', (r, at) => r.view.dispatch({ selection: { anchor: at }, userEvent: 'select.pointer' })]
	];

	for (const [kind, block] of BLOCKS) {
		const doc = `# Plan\n\n${THREEROW}\n${block}\n`;
		const at = doc.indexOf(block);
		const want = `# Plan\n\n${THREEROW}\n\nx\n\n${block}\n`;
		it(`GFM ends the table at ${kind} with no blank line, and reads x typed at its start as more rows`, () => {
			expect(tables(doc)[0].rows.length).toBe(ROWS);
			expect(blocks(doc)).toContain(blocks(`${block}\n`)[0]);
			expect(tables(`${doc.slice(0, at)}x${doc.slice(at)}`)[0].rows.length, 'the hazard').toBeGreaterThan(ROWS);
		});
		for (const [name, leave] of routes) {
			it(`after ${name}, x typed at the start of ${kind} under the table is its own paragraph and the block is unchanged`, () => {
				const r = track(build(doc));
				leave(r, at);
				expect(mountedEditor(r), 'no cell is open').toBeNull();
				expect(r.view.state.doc.toString(), 'arriving changes nothing').toBe(doc);
				expect(r.view.state.selection.main.head, `the caret is at the start of ${kind}`).toBe(at);
				type(r, 'x');
				const text = r.view.state.doc.toString();
				expect(text).toBe(want);
				expect(tables(text)[0].rows.length, 'the table keeps its rows').toBe(ROWS);
				expect(paragraphs(text)).toContain('x');
				for (const b of blocks(doc)) expect(blocks(text), `${b} is unchanged`).toContain(b);
				expect(r.view.state.selection.main.head, 'the caret follows the typed text').toBe(want.indexOf('x\n') + 1);
				type(r, 'y');
				expect(r.view.state.doc.toString(), 'typing on adds nothing more').toBe(want.replace('x\n', 'xy\n'));
			});
		}
		it(`at the start of ${kind}, the typed text and both blank lines are one undo step`, () => {
			const r = track(build(doc));
			routes[0][1](r, at);
			type(r, 'x');
			r.view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 'z', [MOD]: true, bubbles: true, cancelable: true }));
			expect(r.view.state.doc.toString()).toBe(doc);
		});
	}

	it('a paste ending in a line break at the start of a heading adds one more break before it', () => {
		const doc = `${THREEROW}\n## Heading\n`;
		const r = track(build(doc));
		routes[3][1](r, doc.indexOf('## Heading'));
		type(r, 'pasted\n', 'input.paste');
		expect(r.view.state.doc.toString()).toBe(`${THREEROW}\n\npasted\n\n## Heading\n`);
	});

	it('typing inside the block, not at its start, is ordinary editing of that block', () => {
		const doc = `${THREEROW}\n## Heading\n`;
		const r = track(build(doc));
		r.view.dispatch({ selection: { anchor: doc.length - 1 } });
		type(r, 's');
		expect(r.view.state.doc.toString()).toBe(`${THREEROW}\n## Headings\n`);
	});
});
