import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { A as AUTHOR, type Rig } from './typed-reveal-rig';
import {
	cellCaret, cellEditor, cellEl, cellText, caretAt, cellFrom, cleanup, clicked, deleted, DOC, key,
	mountedEditor, open, pasted, press, table, typed
} from './widget-cell-rig';

/**
 * Editing a rendered table cell: the click opens an editor inside the cell's own `<td>` whose document is that cell's
 * source slice, so typing, deleting, pasting, undoing and a live edit from another client all land in the main document,
 * in the cell's range, and the table stays a table while it happens.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

/** Where a cell offset sits in the document, before anything was edited. */
const at = (offset: number) => cellFrom() + offset;

describe('clicking a cell edits it where it is read', () => {
	it('opens an editor inside the cell holding that cell\'s own source, and leaves the table rendered', () => {
		const r = track(open());
		expect(table(r), 'setup: the table is rendered').not.toBeNull();
		clicked(r);
		expect(cellEditor(r), 'the cell editor opened').not.toBeNull();
		expect(cellText(r)).toBe(' Login ');
		expect(cellEl(r).contains(cellEditor(r)!.dom), 'it is the cell that holds the editor').toBe(true);
		expect(table(r), 'the table is still a table').not.toBeNull();
		expect(r.view.state.doc.toString(), 'nothing is written by opening').toBe(DOC);
	});

	it('gives the cell back its rendered text when the cell closes', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		expect(cellText(r), 'while it is open the editor is the cell\'s content').toBe(' LoginXY ');
		press(r, 'Escape');
		expect(mountedEditor(r), 'the editor is gone').toBeNull();
		expect(cellEl(r)!.textContent, 'the cell is rendered text again, with what was typed').toBe('LoginXY');
	});

	it('closes when the reader presses outside the cell', () => {
		const r = track(open());
		clicked(r);
		expect(cellEditor(r)).not.toBeNull();
		r.view.dom.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
		expect(mountedEditor(r)).toBeNull();
		expect(r.view.state.doc.toString()).toBe(DOC);
	});

	it('opens nothing in a read-only document', () => {
		const r = track(open([EditorState.readOnly.of(true)]));
		clicked(r);
		expect(mountedEditor(r)).toBeNull();
	});

	it('keeps the table\'s DOM across a keystroke, so the editor and the reader\'s place survive', () => {
		const r = track(open());
		clicked(r);
		const drawn = table(r), cell = cellEl(r);
		typed(r, 6, 'XY');
		expect(table(r), 'the table widget was not thrown away').toBe(drawn);
		expect(cellEl(r), 'the cell is the same node, holding the same editor').toBe(cell);
		expect(mountedEditor(r), 'and the editor is still mounted in it').toBe(cell.querySelector('.cm-md-cell-editor'));
		expect(cellEl(r, 1, 1).textContent, 'the cell beside it shows the document as it stands').toBe('Done');
	});
});

describe('what is typed in a cell lands in that cell', () => {
	it('writes the cell\'s own range and leaves the rest of the document byte for byte as it was', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		const text = r.view.state.doc.toString();
		expect(text).toBe(`${DOC.slice(0, at(6))}XY${DOC.slice(at(6))}`);
		expect(text, 'the row still has its two columns').toContain('| LoginXY | Done |');
		expect(r.text.toString(), 'the Yjs type agrees with the editor').toBe(text);
	});

	it('sends a delete in the cell to the same range', () => {
		const r = track(open());
		clicked(r);
		deleted(r, 1, 6);
		const text = r.view.state.doc.toString();
		expect(text).toBe(`${DOC.slice(0, at(1))}${DOC.slice(at(6))}`);
		expect(text).toContain('|  | Done |');
		expect(r.text.toString()).toBe(text);
	});

	it('keeps landing in the same cell after the text above the table changes', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		r.view.dispatch({ changes: { from: 0, insert: '# Draft\n\n' }, userEvent: 'input.type' });
		typed(r, 8, '!');
		const text = r.view.state.doc.toString();
		expect(text.startsWith('# Draft\n\n# Plan'), 'the table moved down').toBe(true);
		expect(text).toContain('| LoginXY! |');
		expect(text.slice(text.indexOf('LoginXY!') + 8), 'the row after the cell is intact').toContain('| Done |');
		expect(cellText(r)).toBe(' LoginXY! ');
	});

	it('opens the empty last cell, written as two pipes, and takes what is typed there', () => {
		const r = track(open());
		const edit = '| Login ||';
		r.view.dispatch({ changes: { from: DOC.indexOf('| Login | Done |'), to: DOC.indexOf('| Login | Done |') + '| Login | Done |'.length, insert: edit } });
		clicked(r, table(r)!.rows[1].cells[1]);
		expect(cellText(r), 'the empty cell has nothing of its own yet').toBe('');
		typed(r, 0, 'Z');
		expect(r.view.state.doc.toString()).toContain('| Login |Z|');
		expect(table(r)!.rows[1].cells[1].textContent).toBe('Z');
	});

	it('opens a cell the row omits entirely and gives it its delimiters when it is typed into', () => {
		const r = track(open());
		const row = '| Login |';
		const from = DOC.indexOf('| Login | Done |');
		r.view.dispatch({ changes: { from, to: from + '| Login | Done |'.length, insert: row } });
		expect(table(r)!.rows[1].cells).toHaveLength(2);
		clicked(r, table(r)!.rows[1].cells[1]);
		expect(cellText(r)).toBe('');
		typed(r, 0, 'Z');
		expect(r.view.state.doc.toString(), 'the row gained its second cell in the one keystroke').toContain('| Login | Z |');
		expect(cellText(r)).toBe(' Z ');
		expect(r.view.state.doc.toString().split('\n')).toHaveLength(DOC.split('\n').length);
	});
});

describe('undo and redo in a cell are the document\'s', () => {
	it('one undo in the cell takes the cell edit out of the document and redo puts it back', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		expect(r.view.state.doc.toString()).toContain('LoginXY');
		key(r, 'z');
		expect(r.view.state.doc.toString(), 'the document is back to what it was').toBe(DOC);
		expect(r.text.toString(), 'and the Yjs document with it').toBe(DOC);
		expect(cellText(r), 'the cell follows the document back').toBe(' Login ');
		key(r, 'y');
		expect(r.view.state.doc.toString()).toContain('LoginXY');
		expect(cellText(r)).toBe(' LoginXY ');
	});

	it('leaves the cell editor open on undo, so the reader stays where they were', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		key(r, 'z');
		expect(mountedEditor(r)).not.toBeNull();
		typed(r, 6, 'Z');
		expect(r.view.state.doc.toString()).toContain('LoginZ');
	});
});

describe('a live edit from another client reaches the open cell', () => {
	it('an edit to another cell lands in the document, and the open cell keeps its text and its caret', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		caretAt(r, 8);
		const caret = cellCaret(r);
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('Done') + 4, 'ED'));
		const text = r.view.state.doc.toString();
		expect(text).toContain('DoneED');
		expect(text).toContain('LoginXY');
		expect(cellText(r), 'the open cell is untouched').toBe(' LoginXY ');
		expect(cellCaret(r), 'the caret did not move').toBe(caret);
		expect(r.text.toString()).toBe(text);
		expect(table(r)!.textContent, 'the other cell shows the edit').toContain('DoneED');
	});

	it('an edit inside the open cell shows there, keeps the reader\'s place, and is neither lost nor duplicated', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		caretAt(r, 8);
		const caret = cellCaret(r);
		// Written at the head of the cell, strictly before the reader's place. Two clients landing on one offset is a
		// Yjs tie, and the winner is picked by client id: that would make this test a coin toss, not a check.
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('Login'), 'Z'));
		expect(cellText(r), 'the open cell shows the edit').toBe(' ZLoginXY ');
		expect(cellCaret(r), 'the reader stays in their own text, ahead of the cell\'s padding').toBe(caret + 1);
		const text = r.view.state.doc.toString();
		expect(text.match(/XY/g), 'nothing duplicated').toHaveLength(1);
		expect(text.match(/Z/g), 'nothing duplicated').toHaveLength(1);
		expect(r.text.toString(), 'the document and the Yjs type agree').toBe(text);
	});

	it('typing in the cell after a remote edit lands in that cell', () => {
		const r = track(open());
		clicked(r);
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('Done') + 4, 'ED'));
		typed(r, 6, '!');
		expect(r.view.state.doc.toString()).toContain('Login!');
		expect(r.view.state.doc.toString()).toContain('DoneED');
		expect(cellText(r)).toBe(' Login! ');
	});
});

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
		expect(table(r)!.rows[1].cells).toHaveLength(2);
	});
});

describe('a selection in the cell is a selection in the document', () => {
	it('mirrors the cell selection onto the same source range, as a comment needs it', () => {
		const r = track(open());
		clicked(r);
		cellEditor(r)!.dispatch({ selection: { anchor: 1, head: 4 } });
		expect(r.view.state.selection.main.from, 'the document holds the same range').toBe(at(1));
		expect(r.view.state.selection.main.to).toBe(at(4));
		expect(r.view.state.sliceDoc(r.view.state.selection.main.from, r.view.state.selection.main.to)).toBe('Log');
	});

	it('keeps mirroring the reader\'s place while they type', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, 'XY');
		expect(r.view.state.selection.main.head, 'the caret in the document is where it is in the cell').toBe(at(8));
	});
});
