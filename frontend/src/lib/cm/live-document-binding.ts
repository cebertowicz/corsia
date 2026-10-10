import { keymap } from '@codemirror/view';
import { yCollab, yUndoManagerKeymap } from 'y-codemirror.next';
import * as Y from 'yjs';

/** Bind sync and local undo; presence-marks remains the only caret renderer. */
export function liveDocumentBinding(text: Y.Text) {
	const undo = new Y.UndoManager(text);
	return {
		extensions: [yCollab(text, null, { undoManager: undo }), keymap.of(yUndoManagerKeymap)],
		destroy: () => undo.destroy(),
	};
}
