import { Facet, type Extension } from '@codemirror/state';
import { EditorView, type ViewUpdate } from '@codemirror/view';
import { cellKeys, type CellMoves } from './widget-cell-keys';
import { CELL_THEME } from './widget-cell-theme';
import { cellHighlight } from './theme';
import { INLINE_MARKDOWN } from './widget-cell-markdown';
import { cellMarksField } from './widget-cell-comment';
import { imageContext } from './markdown-image';

/** A cell editor's added extensions: the hide-syntax pass (`markdown-live.ts`) and the save key (`MarkdownEditor.svelte`), as cell keys skip the doc's keymap. */
export const cellEditorExtensions = Facet.define<Extension, readonly Extension[]>({ combine: (values) => values });

/** The cell's own editor: an inline markdown parse, the document's hide-syntax pass, the cell's keys, and no history of its own. */
export function cellEditorSetup(host: EditorView, moves: CellMoves, onUpdate: (update: ViewUpdate) => void): Extension {
	return [
		INLINE_MARKDOWN,
		// The document's own image paths and sign-in, so an image in the cell resolves as it does when drawn.
		imageContext.of(host.state.facet(imageContext)),
		...host.state.facet(cellEditorExtensions),
		cellHighlight,
		cellMarksField,
		EditorView.lineWrapping,
		EditorView.updateListener.of(onUpdate),
		cellKeys(host, moves),
		CELL_THEME
	];
}
