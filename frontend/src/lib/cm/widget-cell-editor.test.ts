import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { forceParsing } from '@codemirror/language';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdownLivePreview } from './markdown-live';
import { A, trackRigs, type Rig } from './typed-reveal-rig';

/**
 * Editing a rendered table cell: a click opens an editor over the cell whose document is that cell's own source slice, so
 * typing, deleting, undoing and a live edit from another client all land in the main document, in the cell's range.
 * The yCollab rig is `typed-reveal.test.ts`'s, so `r.edit(A, …)` is what an agent's live edit looks like to this client.
 */

const LINE = '| Login | Done |';
const DOC = `# Plan\n\n| Feature | Status |\n| --- | --- |\n${LINE}\n\nafter\n`;
/** The body row's first cell, between its pipes: ` Login `, padding included. */
const cellFrom = () => DOC.indexOf(LINE) + 1;
const cellTo = () => cellFrom() + 7;
/** Where a cell offset sits in the document, before anything was edited. */
const at = (offset: number) => cellFrom() + offset;

/** A document with a rendered table, yCollab-bound, with the cell editor installed. */
function open(extensions: Extension[] = []) {
	const r = make(DOC, [markdown({ extensions: GFM }), markdownLivePreview, ...extensions]);
	force(r);
	return r;
}
const { make, cleanup } = trackRigs();
const rigs: Rig[] = [];
function force(r: Rig) {
	if (!forceParsing(r.view, r.view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	rigs.push(r);
}

const cellEl = (r: Rig) => r.view.dom.querySelector('table')!.rows[1].cells[0];
const overlay = () => document.querySelector<HTMLElement>('.cm-md-cell-editor');
const cellEditor = () => {
	const el = overlay()?.querySelector<HTMLElement>('.cm-editor');
	return el ? EditorView.findFromDOM(el) : null;
};
const cellText = () => cellEditor()!.state.doc.toString();
const cellCaret = () => cellEditor()!.state.selection.main.head;
/** The reader's caret, where a click inside the cell would have put it. */
const caretAt = (offset: number) => cellEditor()!.dispatch({ selection: { anchor: offset } });
const clicked = (r: Rig, cell = cellEl(r)) => cell.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
const typed = (from: number, insert: string) => cellEditor()!.dispatch({ changes: { from, to: from, insert }, userEvent: 'input.type' });
const deleted = (from: number, to: number) => cellEditor()!.dispatch({ changes: { from, to, insert: '' }, userEvent: 'delete.content.back' });
// `w3c-keyname` picks Cmd on a Mac platform and Ctrl everywhere else, and that is what a keybinding named Mod-z matches.
// Redo is pressed as Ctrl+Y: jsdom reports Ctrl+Shift+Z with a lowercase key, which CodeMirror reads as plain Mod-z,
// and `yUndoManagerKeymap` binds Ctrl+Y to the same redo command a Mac's Cmd+Shift+Z runs.
const MOD = /Mac/.test(navigator.platform) ? 'metaKey' : 'ctrlKey';
const key = (keyName: string, shift = false) => cellEditor()!.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: keyName, shiftKey: shift, [MOD]: true, bubbles: true, cancelable: true }));

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

describe('clicking a cell edits it where it is read', () => {
	it('opens an editor over the cell holding that cell\'s own source, and leaves the table rendered', () => {
		const r = open();
		expect(r.view.dom.querySelector('table'), 'setup: the table is rendered').not.toBeNull();
		clicked(r);
		expect(cellEditor(), 'the cell editor opened').not.toBeNull();
		expect(cellText()).toBe(' Login ');
		expect(r.view.dom.querySelector('table'), 'the table is still a table').not.toBeNull();
		expect(r.view.state.doc.toString(), 'nothing is written by opening').toBe(DOC);
	});

	it('opens nothing in a read-only document', () => {
		const r = open([EditorState.readOnly.of(true)]);
		clicked(r);
		expect(overlay()).toBeNull();
	});

	it('closes when the reader presses outside the cell', () => {
		const r = open();
		clicked(r);
		expect(cellEditor()).not.toBeNull();
		r.view.dom.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
		expect(cellEditor()).toBeNull();
	});
});

describe('typing in a cell changes the document, and only that cell', () => {
	it('sends a typed word to the cell\'s own range and leaves the rest of the document byte for byte as it was', () => {
		const r = open();
		clicked(r);
		typed(6, 'XY');
		const text = r.view.state.doc.toString();
		expect(text).toBe(`${DOC.slice(0, at(6))}XY${DOC.slice(at(6))}`);
		expect(text.slice(0, cellFrom()), 'the source before the cell is untouched').toBe(DOC.slice(0, cellFrom()));
		expect(text.slice(cellTo() + 2), 'the source after the cell is untouched').toBe(DOC.slice(cellTo()));
		expect(r.text.toString(), 'the Yjs document holds the same text').toBe(text);
		expect(r.view.dom.querySelector('table')!.textContent, 'the table re-renders with it').toContain('LoginXY');
		expect(cellText(), 'the cell editor holds what the document holds').toBe(' LoginXY ');
	});

	it('sends a delete in the cell to the same range', () => {
		const r = open();
		clicked(r);
		deleted(1, 6);
		const text = r.view.state.doc.toString();
		expect(text).toBe(`${DOC.slice(0, at(1))}${DOC.slice(at(6))}`);
		expect(text).toContain('|  | Done |');
		expect(r.text.toString()).toBe(text);
	});

	it('keeps landing in the same cell after the text above the table changes', () => {
		const r = open();
		clicked(r);
		typed(6, 'XY');
		r.view.dispatch({ changes: { from: 0, insert: '# Draft\n\n' }, userEvent: 'input.type' });
		typed(8, '!');
		const text = r.view.state.doc.toString();
		expect(text.startsWith('# Draft\n\n# Plan'), 'the table moved down').toBe(true);
		expect(text).toContain('| LoginXY! |');
		expect(text.slice(text.indexOf('LoginXY!') + 8), 'the row after the cell is intact').toContain('| Done |');
		expect(cellText()).toBe(' LoginXY! ');
	});
});

describe('undo and redo in a cell are the document\'s', () => {
	it('one undo in the cell takes the cell edit out of the document and redo puts it back', () => {
		const r = open();
		clicked(r);
		typed(6, 'XY');
		expect(r.view.state.doc.toString()).toContain('LoginXY');
		key('z');
		expect(r.view.state.doc.toString(), 'the document is back to what it was').toBe(DOC);
		expect(r.text.toString(), 'and the Yjs document with it').toBe(DOC);
		expect(cellText(), 'the cell follows the document back').toBe(' Login ');
		key('y');
		expect(r.view.state.doc.toString()).toContain('LoginXY');
		expect(cellText()).toBe(' LoginXY ');
	});

	it('leaves the cell editor open on undo, so the reader stays where they were', () => {
		const r = open();
		clicked(r);
		typed(6, 'XY');
		key('z');
		expect(cellEditor()).not.toBeNull();
		typed(6, 'Z');
		expect(r.view.state.doc.toString()).toContain('LoginZ');
	});
});

describe('a live edit from another client reaches the open cell', () => {
	it('an edit to another cell lands in the document, and the open cell keeps its text and its caret', () => {
		const r = open();
		clicked(r);
		typed(6, 'XY');
		caretAt(8);
		const caret = cellCaret();
		r.edit(A, (text) => text.insert(text.toString().indexOf('Done') + 4, 'ED'));
		const text = r.view.state.doc.toString();
		expect(text).toContain('DoneED');
		expect(text).toContain('LoginXY');
		expect(cellText(), 'the open cell is untouched').toBe(' LoginXY ');
		expect(cellCaret(), 'the caret did not move').toBe(caret);
		expect(r.text.toString()).toBe(text);
		expect(r.view.dom.querySelector('table')!.textContent, 'the other cell shows the edit').toContain('DoneED');
	});

	it('an edit inside the open cell shows there, keeps the reader\'s place, and is neither lost nor duplicated', () => {
		const r = open();
		clicked(r);
		typed(6, 'XY');
		caretAt(8);
		const caret = cellCaret();
		// Written at the head of the cell, strictly before the reader's place. Two clients landing on one offset is a
		// Yjs tie, and the winner is picked by client id: that would make this test a coin toss, not a check.
		r.edit(A, (text) => text.insert(text.toString().indexOf('Login'), 'Z'));
		expect(cellText(), 'the open cell shows the edit').toBe(' ZLoginXY ');
		expect(cellCaret(), 'the reader stays in their own text, ahead of the cell\'s padding').toBe(caret + 1);
		const text = r.view.state.doc.toString();
		expect(text.match(/XY/g), 'nothing duplicated').toHaveLength(1);
		expect(text.match(/Z/g), 'nothing duplicated').toHaveLength(1);
		expect(r.text.toString(), 'the document and the Yjs type agree').toBe(text);
	});

	it('typing in the cell after a remote edit lands in that cell', () => {
		const r = open();
		clicked(r);
		r.edit(A, (text) => text.insert(text.toString().indexOf('Done') + 4, 'ED'));
		typed(6, '!');
		expect(r.view.state.doc.toString()).toContain('Login!');
		expect(r.view.state.doc.toString()).toContain('DoneED');
		expect(cellText()).toBe(' Login! ');
	});
});

describe('what a cell accepts cannot break the table', () => {
	it('stores a typed pipe escaped, so the row keeps its columns', () => {
		const r = open();
		clicked(r);
		typed(6, '|no');
		const text = r.view.state.doc.toString();
		expect(text).toContain('Login\\|no');
		expect(text).not.toContain('Login|no');
		expect(r.view.dom.querySelector('table')!.rows[1].cells, 'the row is still two columns').toHaveLength(2);
		expect(cellText(), 'the cell editor holds the escaped source too').toBe(' Login\\|no ');
	});

	it('deletes both halves of an escaped pipe together', () => {
		const r = open();
		clicked(r);
		typed(6, '|');
		expect(r.view.state.doc.toString()).toContain('Login\\|');
		deleted(6, 7);
		expect(r.view.state.doc.toString()).toBe(DOC);
		expect(r.view.dom.querySelector('table')!.rows[1].cells).toHaveLength(2);
	});

	it('has nowhere for a line break to go', () => {
		const r = open();
		clicked(r);
		typed(6, '\n');
		expect(r.view.state.doc.toString()).toBe(DOC);
		expect(cellText()).toBe(' Login ');
	});
});
