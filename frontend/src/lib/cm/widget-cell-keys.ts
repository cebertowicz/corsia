import { defaultKeymap } from '@codemirror/commands';
import { keymap, type EditorView } from '@codemirror/view';
import { yUndoManagerKeymap } from 'y-codemirror.next';

/** Where the keys that mean something different in a cell take the reader; the open cell's editor supplies each move. */
export type CellMoves = { leave: () => void; next: () => void; previous: () => void; below: () => void };

/** The keys of a cell's editor: moving around and out of the table, the document's undo, and the ordinary caret keys. */
export const cellKeys = (host: EditorView, moves: CellMoves) => keymap.of([
	{ key: 'Escape', preventDefault: true, run: () => (moves.leave(), true) },
	{ key: 'Tab', preventDefault: true, run: () => (moves.next(), true) },
	{ key: 'Shift-Tab', preventDefault: true, run: () => (moves.previous(), true) },
	{ key: 'Enter', preventDefault: true, run: () => (moves.below(), true) },
	// Undo and redo are the document's own: the same commands the document's keymap runs, against the
	// document's view, so the one Yjs undo manager holds the cell's edits in the document's history.
	...yUndoManagerKeymap.map((binding) => ({ ...binding, run: () => binding.run?.(host) ?? false })),
	// The ordinary caret keys — arrows, Home, End, deleting forward — are the platform's own bindings. Only
	// the ones above are given a different meaning inside a cell.
	...defaultKeymap
]);
