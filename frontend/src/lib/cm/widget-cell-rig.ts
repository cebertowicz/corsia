import { forceParsing } from '@codemirror/language';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import { EditorView } from '@codemirror/view';
import type { Extension } from '@codemirror/state';
import { markdownLivePreview } from './markdown-live';
import { trackRigs, type Rig } from './typed-reveal-rig';

/**
 * A yCollab-bound document with a rendered table and the cell editor installed, for the specs in
 * `widget-cell-editor.test.ts` and `widget-cell-nav.test.ts`. `r.edit(A, …)` is what an agent's live edit looks like to
 * this client, and `cellEditor()` is the editor the reader opened inside a cell.
 */

export const LINE = '| Login | Done |';
export const DOC = `# Plan\n\n| Feature | Status |\n| --- | --- |\n${LINE}\n\nafter\n`;
export const THREEROW = '| Feature | Status | Note |\n| --- | --- | --- |\n| Login | Done | first |\n| Sync | Open | last |';

/** The body row's first cell, between its pipes: ` Login `, padding included. */
export const cellFrom = () => DOC.indexOf(LINE) + 1;
export const cellTo = () => cellFrom() + 7;

export const { make, cleanup } = trackRigs();

// jsdom lays nothing out and gives a Range no rects, so the caret a click places has nowhere to be read from; without these,
// CodeMirror's `posAtCoords` throws in the middle of opening a cell and the rest of the open is never run.
const rangeLayout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
rangeLayout.getClientRects ??= () => [] as unknown as DOMRectList;
rangeLayout.getBoundingClientRect ??= () => new DOMRect();

export function build(doc: string, extensions: Extension[] = []) {
	const r = make(doc, [markdown({ extensions: GFM }), markdownLivePreview, ...extensions]);
	if (!forceParsing(r.view, r.view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	return r;
}
export const open = (extensions: Extension[] = []) => build(DOC, extensions);

export const table = (r: Rig) => r.view.dom.querySelector('table');
export const cellEl = (r: Rig, row = 1, col = 0) => table(r)!.rows[row].cells[col];
// One cell is open at a time, wherever it is in the table: the editor is in the layer, the layer is over the cell.
export const mountedEditor = (r: Rig) => r.view.dom.querySelector<HTMLElement>('.cm-md-cell-editor');
export const layer = (r: Rig) => r.view.dom.querySelector<HTMLElement>('.cm-md-cell-layer');
export const editingCell = (r: Rig) => r.view.dom.querySelector<HTMLTableCellElement>('.cm-md-cell-editing');
export const cellEditor = (r: Rig) => {
	const el = mountedEditor(r)?.querySelector<HTMLElement>('.cm-editor');
	return el ? EditorView.findFromDOM(el) : null;
};
export const cellText = (r: Rig) => cellEditor(r)!.state.doc.toString();
export const cellCaret = (r: Rig) => cellEditor(r)!.state.selection.main.head;
/** The reader's place inside the cell, where a click would have put it. */
export const caretAt = (r: Rig, offset: number) => cellEditor(r)!.dispatch({ selection: { anchor: offset } });
export const clicked = (r: Rig, cell = cellEl(r)) => cell.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
// A browser puts the caret after what was typed and where the delete happened, so the specs do the same.
export const typed = (r: Rig, from: number, insert: string) =>
	cellEditor(r)!.dispatch({ changes: { from, to: from, insert }, selection: { anchor: from + insert.length }, userEvent: 'input.type' });
export const pasted = (r: Rig, from: number, insert: string) =>
	cellEditor(r)!.dispatch({ changes: { from, to: from, insert }, selection: { anchor: from + insert.length }, userEvent: 'paste' });
export const deleted = (r: Rig, from: number, to: number) =>
	cellEditor(r)!.dispatch({ changes: { from, to, insert: '' }, selection: { anchor: from }, userEvent: 'delete.content.back' });
// `w3c-keyname` picks Cmd on a Mac platform and Ctrl everywhere else, and that is what a keybinding named Mod-z matches.
// Redo is pressed as Ctrl+Y: jsdom reports Ctrl+Shift+Z with a lowercase key, which CodeMirror reads as plain Mod-z,
// and `yUndoManagerKeymap` binds Ctrl+Y to the same redo command a Mac's Cmd+Shift+Z runs.
export const MOD = /Mac/.test(navigator.platform) ? 'metaKey' : 'ctrlKey';
export const key = (r: Rig, keyName: string, shift = false) =>
	cellEditor(r)!.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: keyName, shiftKey: shift, [MOD]: true, bubbles: true, cancelable: true }));
export const press = (r: Rig, keyName: string, shift = false) =>
	cellEditor(r)!.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: keyName, shiftKey: shift, bubbles: true, cancelable: true }));
