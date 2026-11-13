import { defaultKeymap, historyField, redo, undo } from '@codemirror/commands';
import type { Command } from '@codemirror/view';
import { keymap, type EditorView } from '@codemirror/view';
import { yUndoManagerKeymap } from 'y-codemirror.next';
import { openCellState } from './widget-cell-state';

/** Where the keys that mean something different in a cell take the reader; the open cell's editor supplies each move. */
export type CellMoves = { leave: () => void; next: () => void; previous: () => void; below: () => void };

const [yUndo, yRedo] = [yUndoManagerKeymap[0].run!, yUndoManagerKeymap[1].run!];

/** The keys of a cell's editor: moving around and out of the table, the document's undo, and the ordinary caret keys. */
export function cellKeys(host: EditorView, moves: CellMoves) {
	// A key that reaches a cell's editor after its cell has closed (the document turned read-only under it) moves nothing
	// and undoes nothing in the document.
	const open = (run: () => boolean) => () => !!host.state.field(openCellState, false) && run();
	// Undo and redo are the document's own, run against the document's view so the cell's edits are in its one history:
	// CodeMirror's history when the document is not live, the shared Yjs undo manager when it is.
	const history = (local: Command, live: Command) => open(() => (host.state.field(historyField, false) ? local : live)(host));
	return keymap.of([
		{ key: 'Escape', preventDefault: true, run: open(() => (moves.leave(), true)) },
		{ key: 'Tab', preventDefault: true, run: open(() => (moves.next(), true)) },
		{ key: 'Shift-Tab', preventDefault: true, run: open(() => (moves.previous(), true)) },
		{ key: 'Enter', preventDefault: true, run: open(() => (moves.below(), true)) },
		{ key: 'Mod-z', preventDefault: true, run: history(undo, yUndo) },
		{ key: 'Mod-y', mac: 'Mod-Shift-z', preventDefault: true, run: history(redo, yRedo) },
		{ key: 'Mod-Shift-z', preventDefault: true, run: history(redo, yRedo) },
		// The ordinary caret keys — arrows, Home, End, deleting forward — are the platform's own bindings. Only
		// the ones above are given a different meaning inside a cell.
		...defaultKeymap
	]);
}
