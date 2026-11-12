import type { EditorView } from '@codemirror/view';
import { drawnTableAt } from './widget-blocks';

/** The widest the Comment offer is (on a phone), and the gap kept around it. */
export const OFFER_GAP = 6;
export const OFFER_ROOM = 120 + 2 * OFFER_GAP;
// Under the table the offer has the table's own bottom margin to fit in, about the offer's height and a few pixels more.
const BELOW_GAP = 3;

type Words = { top: number; bottom: number; right: number };
export type OfferPlace = { top: number; left: number; exact?: true; below?: true };

/**
 * The one rule for where the Comment offer for selected words goes, in prose and in an open table cell alike: beside where
 * the words end, so it covers no text. In a table the place beside the words is the cell's own text or the next cell's, so
 * there the free space beside them is the margin beside the table, level with the words' line, or, where the table leaves
 * no margin, directly below it. `from` is where the words start in the document; `words` is the box of their last line.
 */
export function offerPlace(view: EditorView, from: number, words: Words): OfferPlace {
	const widget = drawnTableAt(view, from);
	if (!widget) return { top: words.top, left: words.right };
	// The table's own box: the widget around it carries padding the offer must not sit in, with the text below.
	const table = (widget.querySelector('table') ?? widget).getBoundingClientRect(), column = view.scrollDOM.getBoundingClientRect();
	if (column.right - table.right >= OFFER_ROOM) return { exact: true, top: (words.top + words.bottom) / 2, left: table.right + OFFER_GAP };
	return { exact: true, below: true, top: table.bottom + BELOW_GAP, left: table.left };
}
