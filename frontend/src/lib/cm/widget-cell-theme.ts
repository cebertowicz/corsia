import { EditorView } from '@codemirror/view';

/** The theme a cell editor needs to read as the cell it sits in; its type and colour come from the cell around it. */
export const CELL_THEME = EditorView.theme({
	'&': { backgroundColor: 'transparent' },
	'.cm-scroller': { overflow: 'hidden', fontFamily: 'inherit', lineHeight: 'inherit' },
	'.cm-content': { padding: '0', caretColor: 'var(--accent)' },
	'.cm-line': { padding: '0' },
	'&.cm-focused': { outline: 'none' }
});
