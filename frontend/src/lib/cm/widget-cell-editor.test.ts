import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Compartment, EditorState } from '@codemirror/state';
import * as productEvents from '$lib/product_events';
import { A as AUTHOR, type Rig } from './typed-reveal-rig';
import {
	build, cellCaret, cellEditor, cellEl, cellText, caretAt, cellFrom, cleanup, clicked, deleted, DOC, editingCell, key,
	mountedEditor, open, press, table, typed
} from './widget-cell-rig';
import { openCellState } from './widget-cell-state';

/**
 * Editing a rendered table cell: the click opens an editor in the cell's own `<td>` whose document is that cell's
 * content, so typing, deleting, pasting, undoing and a live edit from another client all land in the main document,
 * in the cell's range, and the table stays a table while it happens.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

/** Where a cell offset sits in the document, before anything was edited. */
const at = (offset: number) => cellFrom() + offset;

describe('clicking a cell edits it where it is read', () => {
	it('opens an editor in the cell holding that cell\'s own source, and leaves the table rendered', () => {
		const r = track(open());
		expect(table(r), 'setup: the table is rendered').not.toBeNull();
		clicked(r);
		expect(cellEditor(r), 'the cell editor opened').not.toBeNull();
		expect(cellText(r), 'the cell\'s content as the table draws it, without the pads around it').toBe('Login');
		expect(editingCell(r), 'the cell it is in is the one clicked').toBe(cellEl(r));
		expect(Array.from(cellEl(r).childNodes), 'and it is all the cell holds while open').toEqual([cellEditor(r)!.dom.parentElement]);
		expect(cellEditor(r)!.dom.contains(document.activeElement), 'the click put the caret in the cell').toBe(true);
		expect(table(r), 'the table is still a table').not.toBeNull();
		expect(r.view.state.doc.toString(), 'nothing is written by opening').toBe(DOC);
	});

	it('gives the cell back its rendered text when the cell closes', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		expect(cellText(r), 'while it is open the editor is the cell\'s content').toBe('LoginXY');
		press(r, 'Escape');
		expect(mountedEditor(r), 'the editor is gone').toBeNull();
		expect(cellEl(r).querySelector('.cm-editor'), 'and the cell holds none of it').toBeNull();
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

	it('closes, keeping what was typed, when the document turns read-only, and sends it nothing more', () => {
		const readOnly = new Compartment();
		const r = track(open([readOnly.of(EditorState.readOnly.of(false))]));
		clicked(r);
		typed(r, 5, 'XY');
		const stale = cellEditor(r)!;
		// Only the read-only setting changes, with no change to the text, as the viewer does while a suggestion is applied.
		r.view.dispatch({ effects: readOnly.reconfigure(EditorState.readOnly.of(true)) });
		const kept = DOC.replace('Login', 'LoginXY');
		expect(mountedEditor(r), 'the cell closed').toBeNull();
		expect(r.view.state.doc.toString(), 'what was typed before is kept').toBe(kept);
		expect(cellEl(r).textContent).toBe('LoginXY');
		const sent = vi.spyOn(r.view, 'dispatch'), thrown = vi.fn();
		window.addEventListener('error', thrown);
		stale.dispatch({ changes: { from: 7, insert: 'Z' }, userEvent: 'input.type' });
		for (const key of ['Tab', 'Enter', 'Escape']) stale.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
		window.removeEventListener('error', thrown);
		expect(sent, 'no keystroke after the switch is sent to the document').not.toHaveBeenCalled();
		expect(thrown, 'and none of them fails on the closed cell').not.toHaveBeenCalled();
		expect(r.view.state.doc.toString()).toBe(kept);
	});

	it('reports one committed edit per cell left changed, and none for a cell only looked at', () => {
		const emit = vi.spyOn(productEvents, 'emitProductEvent');
		const r = track(open());
		clicked(r);
		press(r, 'Escape');
		expect(emit, 'opening and leaving a cell is not an edit').not.toHaveBeenCalled();
		clicked(r);
		expect(r.view.dom.querySelectorAll('.cm-md-cell-editor'), 'the closed editor left nothing behind').toHaveLength(1);
		typed(r, 5, 'X');
		typed(r, 6, 'Y');
		press(r, 'Tab');
		expect(emit.mock.calls, 'moving on to the next cell commits the one left').toEqual([['document', 'table_cell_edited']]);
		press(r, 'Escape');
		expect(emit, 'the next cell was not changed').toHaveBeenCalledTimes(1);
		emit.mockRestore();
	});

	it('reports no edit for a cell left with the text it opened with, after typing and deleting back or undoing', () => {
		const emit = vi.spyOn(productEvents, 'emitProductEvent');
		const r = track(open());
		clicked(r);
		typed(r, 5, 'X');
		deleted(r, 5, 6);
		press(r, 'Escape');
		expect(emit, 'typed, then deleted back').not.toHaveBeenCalled();
		clicked(r);
		typed(r, 5, 'XY');
		key(r, 'z');
		expect(cellText(r), 'setup: the undo took the typing out').toBe('Login');
		press(r, 'Tab');
		expect(emit, 'typed, then undone').not.toHaveBeenCalled();
		emit.mockRestore();
	});

	it('keeps the table\'s DOM across a keystroke, so the editor and the reader\'s place survive', () => {
		const r = track(open());
		clicked(r);
		const drawn = table(r), cell = cellEl(r), editor = mountedEditor(r);
		typed(r, 5, 'XY');
		expect(table(r), 'the table widget was not thrown away').toBe(drawn);
		expect(cellEl(r), 'the cell is the same node, still the one being edited').toBe(cell);
		expect(mountedEditor(r), 'and the same editor is still in it').toBe(editor);
		expect(cellEl(r, 1, 1).textContent, 'the cell beside it shows the document as it stands').toBe('Done');
	});
});

describe('what is typed in a cell lands in that cell', () => {
	it('writes the cell\'s own range and leaves the rest of the document byte for byte as it was', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		const text = r.view.state.doc.toString();
		expect(text).toBe(`${DOC.slice(0, at(6))}XY${DOC.slice(at(6))}`);
		expect(text, 'the row still has its two columns').toContain('| LoginXY | Done |');
		expect(r.text.toString(), 'the Yjs type agrees with the editor').toBe(text);
	});

	it('sends a delete in the cell to the same range', () => {
		const r = track(open());
		clicked(r);
		deleted(r, 0, 5);
		const text = r.view.state.doc.toString();
		expect(text).toBe(`${DOC.slice(0, at(1))}${DOC.slice(at(6))}`);
		expect(text).toContain('|  | Done |');
		expect(r.text.toString()).toBe(text);
	});

	it('keeps landing in the same cell after the text above the table changes', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		r.view.dispatch({ changes: { from: 0, insert: '# Draft\n\n' }, userEvent: 'input.type' });
		typed(r, 7, '!');
		const text = r.view.state.doc.toString();
		expect(text.startsWith('# Draft\n\n# Plan'), 'the table moved down').toBe(true);
		expect(text).toContain('| LoginXY! |');
		expect(text.slice(text.indexOf('LoginXY!') + 8), 'the row after the cell is intact').toContain('| Done |');
		expect(cellText(r)).toBe('LoginXY!');
	});

	it('opens the empty last cell, written as two pipes, and takes what is typed there', () => {
		const r = track(open());
		const edit = '| Login ||';
		r.view.dispatch({ changes: { from: DOC.indexOf('| Login | Done |'), to: DOC.indexOf('| Login | Done |') + '| Login | Done |'.length, insert: edit } });
		clicked(r, table(r)!.rows[1].cells[1]);
		expect(cellText(r), 'the empty cell has nothing of its own yet').toBe('');
		typed(r, 0, 'Z');
		expect(r.view.state.doc.toString()).toContain('| Login | Z |');
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
		expect(cellText(r)).toBe('Z');
		expect(r.view.state.doc.toString().split('\n')).toHaveLength(DOC.split('\n').length);
	});
});

describe('undo and redo in a cell are the document\'s', () => {
	it('one undo in the cell takes the cell edit out of the document and redo puts it back', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		expect(r.view.state.doc.toString()).toContain('LoginXY');
		key(r, 'z');
		expect(r.view.state.doc.toString(), 'the document is back to what it was').toBe(DOC);
		expect(r.text.toString(), 'and the Yjs document with it').toBe(DOC);
		expect(cellText(r), 'the cell follows the document back').toBe('Login');
		key(r, 'y');
		expect(r.view.state.doc.toString()).toContain('LoginXY');
		expect(cellText(r)).toBe('LoginXY');
	});

	it('leaves the cell editor open on undo, so the reader stays where they were', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		key(r, 'z');
		expect(mountedEditor(r)).not.toBeNull();
		typed(r, 5, 'Z');
		expect(r.view.state.doc.toString()).toContain('LoginZ');
	});
});

describe('a live edit from another client reaches the open cell', () => {
	it('an edit to another cell lands in the document, and the open cell keeps its text and its caret', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		caretAt(r, 7);
		const caret = cellCaret(r);
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('Done') + 4, 'ED'));
		const text = r.view.state.doc.toString();
		expect(text).toContain('DoneED');
		expect(text).toContain('LoginXY');
		expect(cellText(r), 'the open cell is untouched').toBe('LoginXY');
		expect(cellCaret(r), 'the caret did not move').toBe(caret);
		expect(r.text.toString()).toBe(text);
		expect(table(r)!.textContent, 'the other cell shows the edit').toContain('DoneED');
	});

	it('an edit inside the open cell shows there, keeps the reader\'s place, and is neither lost nor duplicated', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		caretAt(r, 7);
		const caret = cellCaret(r);
		// Written at the head of the cell, strictly before the reader's place. Two clients landing on one offset is a
		// Yjs tie, and the winner is picked by client id: that would make this test a coin toss, not a check.
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('Login'), 'Z'));
		expect(cellText(r), 'the open cell shows the edit').toBe('ZLoginXY');
		expect(cellCaret(r), 'the reader stays in their own text').toBe(caret + 1);
		const text = r.view.state.doc.toString();
		expect(text.match(/XY/g), 'nothing duplicated').toHaveLength(1);
		expect(text.match(/Z/g), 'nothing duplicated').toHaveLength(1);
		expect(r.text.toString(), 'the document and the Yjs type agree').toBe(text);
	});

	it('an edit to another row arrives while the reader is typing, and the row holds', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('Status') + 6, '!'));
		typed(r, 7, '!');
		const text = r.view.state.doc.toString();
		expect(text).toContain('LoginXY!');
		expect(text).toContain('Status!');
		expect(table(r)!.rows[1].cells, 'the row is still two columns').toHaveLength(2);
		expect(r.text.toString()).toBe(text);
	});

	it('typing in the cell after a remote edit lands in that cell', () => {
		const r = track(open());
		clicked(r);
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('Done') + 4, 'ED'));
		typed(r, 5, '!');
		expect(r.view.state.doc.toString()).toContain('Login!');
		expect(r.view.state.doc.toString()).toContain('DoneED');
		expect(cellText(r)).toBe('Login!');
	});

	for (const [change, edit, row] of [
		['adds a row below it', (text: string) => [text.indexOf('\n\nafter'), `\n| Sync | Open |`], 1],
		['adds a row above it', (text: string) => [text.indexOf('| Login'), `| Sync | Open |\n`], 2]
	] as const) {
		it(`keeps the caret in the open cell when another client ${change}, so typing goes on landing there`, () => {
			const r = track(open());
			clicked(r);
			typed(r, 5, 'XY');
			expect(cellEditor(r)!.hasFocus, 'setup: the reader is typing in the cell').toBe(true);
			r.edit(AUTHOR, (text) => { const [at, insert] = edit(text.toString()); text.insert(at as number, insert as string); });
			expect(editingCell(r), 'the cell is still open, in the table as now drawn').toBe(cellEl(r, row));
			expect(cellEditor(r)!.hasFocus, 'the cell still has the caret').toBe(true);
			expect(cellCaret(r), 'where the reader left it').toBe(7);
			typed(r, cellCaret(r), '!');
			expect(r.view.state.doc.toString()).toContain('| LoginXY! |');
		});
	}
});

describe('a selection in the cell is a selection in the document', () => {
	it('mirrors the cell selection onto the same source range, as a comment needs it', () => {
		const r = track(open());
		clicked(r);
		cellEditor(r)!.dispatch({ selection: { anchor: 0, head: 3 } });
		expect(r.view.state.selection.main.from, 'the document holds the same range').toBe(at(1));
		expect(r.view.state.selection.main.to).toBe(at(4));
		expect(r.view.state.sliceDoc(r.view.state.selection.main.from, r.view.state.selection.main.to)).toBe('Log');
	});

	it('keeps mirroring the reader\'s place while they type', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		expect(r.view.state.selection.main.head, 'the caret in the document is where it is in the cell').toBe(at(8));
		// A caret at the end of a long insertion is past the end of the document until the change lands there, so the
		// document has to take the text before it can take the selection — and a mirrored selection is what a comment needs.
		const long = ' and a great many words after them';
		typed(r, 7, long);
		expect(r.view.state.doc.toString(), 'the whole insertion is in the cell, and the row keeps its columns')
			.toContain(`| LoginXY${long} | Done |`);
		expect(r.view.state.selection.main.head, 'and the document mirrors the caret after it').toBe(at(8 + long.length));
	});
});

describe('a rewrite of the region around the open cell', () => {
	it('closes the cell editor, and takes no cell\'s focus', () => {
		const r = track(build(DOC.replace('| Login | Done |', '| Login | Done |\n| Sync | Open |')));
		clicked(r);
		typed(r, 0, 'Q');
		expect(mountedEditor(r), 'setup: the cell is open').not.toBeNull();
		// Another client rewrites the rows the cell is in, as a delete and an insert in one update.
		const rows = '| Login | Done |\n| Sync | Open |';
		r.edit(AUTHOR, (text) => {
			const row = text.toString().indexOf(rows);
			text.delete(row, rows.length);
			text.insert(row, '| Mail | Sent |\n| Chat | Held |');
		});
		const text = r.view.state.doc.toString();
		expect(text, 'the rewrite is in the document').toContain('| Mail | Sent |\n| Chat | Held |');
		expect(r.view.state.field(openCellState), 'no cell is open').toBeNull();
		expect(mountedEditor(r), 'no editor is left over a cell').toBeNull();
		expect(editingCell(r), 'no cell is marked as being edited').toBeNull();
		expect(document.activeElement?.closest('.cm-md-cell-editor') ?? null, 'no cell editor holds the focus').toBeNull();
		expect(r.text.toString()).toBe(text);
	});

	it('closes it for a whole-document replace, as a reload of the live text is', () => {
		const r = track(open());
		clicked(r);
		typed(r, 5, 'XY');
		r.view.dispatch({ changes: { from: 0, to: r.view.state.doc.length, insert: r.view.state.doc.toString().replace('LoginXY', 'Other') } });
		expect(r.view.state.field(openCellState)).toBeNull();
		expect(mountedEditor(r)).toBeNull();
	});

	it('keeps the cell open for an edit that replaces only its own text', () => {
		const r = track(open());
		clicked(r);
		r.edit(AUTHOR, (text) => {
			const login = text.toString().indexOf('Login');
			text.delete(login, 'Login'.length);
			text.insert(login, 'Mail');
		});
		expect(r.view.state.field(openCellState)?.col).toBe(0);
		expect(cellText(r)).toBe('Mail');
	});
});
