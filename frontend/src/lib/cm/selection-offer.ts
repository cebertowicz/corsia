import type { EditorView } from '@codemirror/view';
import { emptyCellSelection, openCellText } from './widget-cell-comment';
import { widgetCommentLabel } from './widget-cells';
import { offerPlace } from './offer-place';
import type { SelectionOffer } from './widget-selection-types';

export type { SelectionOffer };

/**
 * What the Comment offer is raised for from the editor's own selection: the selected text, placed by the one rule for selected
 * words (`offerPlace`); for a caret in an open empty cell, that whole cell (it has no text to select); null for any other caret.
 */
export function selectionOffer(
	v: EditorView,
	anchorFor: (v: EditorView, from: number, to: number) => () => DOMRect | null,
): SelectionOffer | null {
	const r = v.state.selection.main;
	if (r.empty) return emptyCellSelection(v);
	const text = v.state.sliceDoc(r.from, r.to), { state } = v;
	// Text selected in the open cell is measured in the cell's own editor: the table hides that source from the document's.
	const inCell = openCellText(v, r.from, r.to), coords = inCell ? inCell.inner.coordsAtPos(inCell.at(r.to)) : v.coordsAtPos(r.to);
	return {
		from: r.from, to: r.to, text,
		...(coords ? offerPlace(v, inCell ? r.from : null, coords) : { top: 0, left: 0 }),
		anchor: anchorFor(v, r.from, r.to),
		label: () => widgetCommentLabel(state, r.from, r.to, text) ?? undefined,
	};
}
