import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { forceParsing } from '@codemirror/language';
import { markdown } from '@codemirror/lang-markdown';
import { history, historyKeymap } from '@codemirror/commands';
import { EditorView, keymap } from '@codemirror/view';
import { GFM } from '@lezer/markdown';
import { markdownLivePreview } from './markdown-live';
import type { Rig } from './typed-reveal-rig';
import { cellText, clicked, DOC, key, mountedEditor, typed } from './widget-cell-rig';

/**
 * A document that is not live keeps CodeMirror's own history, not a Yjs undo manager (the editor's local history, used when
 * there is no live text). Undo and redo pressed in an open cell run that history, so the cell's edits come out and go back
 * as they do in prose.
 */

let view: EditorView | null = null;
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { view?.destroy(); view = null; document.body.innerHTML = ''; vi.useRealTimers(); });

function localDocument(): Rig {
	view = new EditorView({
		doc: DOC,
		parent: document.body,
		extensions: [history(), keymap.of(historyKeymap), markdown({ extensions: GFM }), markdownLivePreview],
	});
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	// The cell helpers read only the document's view; this document has no Yjs text to give them.
	return { view } as Rig;
}

describe('undo and redo in a cell of a document that is not live', () => {
	it('run the document\'s own history: undo takes the cell edit out, redo puts it back, and the cell stays open', () => {
		const r = localDocument();
		clicked(r);
		typed(r, 5, 'XY');
		expect(r.view.state.doc.toString(), 'setup: the edit reached the document').toContain('LoginXY');
		key(r, 'z');
		expect(r.view.state.doc.toString(), 'undo takes it out of the document').toBe(DOC);
		expect(cellText(r), 'and the cell follows').toBe('Login');
		expect(mountedEditor(r), 'the cell stays open').not.toBeNull();
		key(r, 'y');
		expect(r.view.state.doc.toString(), 'redo puts it back').toContain('LoginXY');
		expect(cellText(r)).toBe('LoginXY');
	});
});
