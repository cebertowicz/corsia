import type { EditorView } from '@codemirror/view';
import { selectionRange, type CommentSelection } from '$lib/stores/commentSelection';
import { cellTextRows } from './widget-cell-comment';
import { rowsOf } from './comment-anchor-geometry';

/**
 * Where the new-comment card sits. Once its composer is open the draft's own captured range is the answer, because
 * that range already follows every local and remote edit; the selection's offsets are only good until then. A range
 * the draft has lost keeps the card where it was last drawn, so its message stays readable. Text in a table cell (selected in
 * its open editor) anchors as prose does, to the lines its words are drawn on.
 */
export function commentComposerAnchor(v: EditorView, from: number, to: number, pending: () => CommentSelection | null): () => DOMRect | null {
	let drawn: DOMRect | null = null;
	return () => {
		const selected = pending();
		const range = selected ? selectionRange(selected) : { from, to };
		if (!range) return drawn;
		const rows = range.to <= v.state.doc.length ? cellTextRows(v, range.from, range.to) ?? rowsOf(v, range.from, range.to) : null;
		if (rows) drawn = rows;
		return rows;
	};
}
