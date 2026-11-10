import type { EditorView } from '@codemirror/view';
import type { SelectionLabel } from '$lib/stores/commentDraft';
import { emptyCellSelection, openCellText } from './widget-cell-comment';
import { widgetCommentLabel } from './widget-cells';

export interface SelectionOffer {
	from: number; to: number; text: string; top: number; left: number;
	anchor?: () => DOMRect | null;
	label?: SelectionLabel;
	above?: boolean;
	inWidget?: boolean;
}

/**
 * What the Comment offer is raised for from the editor's own selection: the selected text, placed beside its end; for a caret
 * in an open empty cell, that whole cell (it has no text to select); null for any other caret.
 */
export function selectionOffer(
	v: EditorView,
	anchorFor: (v: EditorView, from: number, to: number) => () => DOMRect | null,
): SelectionOffer | null {
	const r = v.state.selection.main;
	if (r.empty) return emptyCellSelection(v);
	const text = v.state.sliceDoc(r.from, r.to), { state } = v;
	// Text selected in the open cell is placed by the same rule, measured in the cell's own editor (the table hides that source from the document's).
	const inCell = openCellText(v, r.from, r.to), coords = inCell ? inCell.inner.coordsAtPos(r.to - inCell.base) : v.coordsAtPos(r.to);
	return {
		from: r.from, to: r.to, text,
		top: coords?.top ?? 0, left: coords?.right ?? coords?.left ?? 0,
		anchor: anchorFor(v, r.from, r.to),
		label: () => widgetCommentLabel(state, r.from, r.to, text) ?? undefined,
	};
}
