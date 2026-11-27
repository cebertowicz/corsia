import { EditorView } from '@codemirror/view';
import { openCellText, textlessCellSelection } from './widget-cell-comment';
import { widgetCommentLabel } from './widget-cells';
import { offerPlace } from './offer-place';
import type { SelectionOffer } from './widget-selection-types';

export type { SelectionOffer };

/**
 * What the Comment offer is raised for from the editor's own selection: the selected text, placed by the one rule for selected
 * words (`offerPlace`); for a caret in an open cell with no text to select, that cell's offer (`textlessCellSelection`: an
 * empty cell's whole cell, or the markdown of the images a cell draws instead of text); null for any other caret.
 */
export function selectionOffer(
	v: EditorView,
	anchorFor: (v: EditorView, from: number, to: number) => () => DOMRect | null,
): SelectionOffer | null {
	const r = v.state.selection.main;
	if (r.empty) return textlessCellSelection(v);
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

/** A kept draft's passage offered as the person's own selection of it would be: selected, and scrolled to the middle of the view. */
export function passageOffer(v: EditorView, from: number, to: number, anchorFor: (v: EditorView, from: number, to: number) => () => DOMRect | null): SelectionOffer | null {
	v.dispatch({ selection: { anchor: from, head: to }, effects: EditorView.scrollIntoView(from, { y: 'center' }) });
	return selectionOffer(v, anchorFor);
}

/** Where a kept draft with no place left opens: at the foot of what the reader sees, so the text they select from stays readable. */
export function footOffer(v: EditorView): SelectionOffer {
	const box = v.scrollDOM.getBoundingClientRect();
	const top = Math.min(box.bottom, window.innerHeight) - 24, left = box.left + box.width / 2;
	return { from: 0, to: 0, text: '', top, left, exact: true, anchor: () => new DOMRect(left, top, 0, 0) };
}
