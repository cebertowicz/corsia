import type { EditorView } from '@codemirror/view';
import { emptyCellSelection, offerBesideTable, openCellText } from './widget-cell-comment';
import { widgetCommentLabel } from './widget-cells';
import type { SelectionOffer } from './widget-selection-types';

export type { SelectionOffer };

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
	const beside = inCell && coords ? offerBesideTable(v, r.from, coords) : null;
	return {
		from: r.from, to: r.to, text,
		top: coords?.top ?? 0, left: coords?.right ?? coords?.left ?? 0,
		...beside,
		anchor: anchorFor(v, r.from, r.to),
		label: () => widgetCommentLabel(state, r.from, r.to, text) ?? undefined,
	};
}
