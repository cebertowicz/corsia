import type { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { blockRange, cellElement, drawnTableAt } from './widget-blocks';
import { CELL_EDITOR, cellSource, openCellState } from './widget-cell-state';
import { tableCells } from './widget-cells';
import { selectionIn } from './widget-selection';
import { acrossColumn } from './comment-anchor-geometry';
import type { WidgetSelection } from './widget-selection-types';

/** A draft's text as drawn in a closed cell. */
const PENDING = '.cm-comment-pending';

/**
 * An empty cell has no text to select, so while its editor is open and still empty the Comment offer is the whole cell's,
 * as it is for a tap on an empty cell of a document that can't be edited. The first character typed makes it a cell with
 * text, where a comment starts from selected text as anywhere else; emptying the cell again brings the offer back.
 */

/** The open cell while it holds no text, or null: no cell open, or the open one has text. */
export function emptyOpenCell(state: EditorState) {
	const cell = state.field(openCellState, false) ?? null;
	return cell && cellSource(state, cell).trim() === '' ? cell : null;
}

/** Which empty cell is open, as a key that changes when another cell opens, so a move between empty cells raises the offer again. */
export const emptyCellKey = (state: EditorState) => {
	const cell = emptyOpenCell(state);
	return cell ? `${cell.tableFrom}:${cell.row}:${cell.col}` : null;
};

/**
 * The whole-cell selection the Comment offer is raised for while the open cell is empty, placed on that cell: above its top
 * edge, from its left. Null otherwise. Its one writer is the editor's own selection flow (`selectionOffer`), which raises it for a
 * caret there and clears it once the cell has text; the widget comment flow reads only selections drawn inside a widget.
 */
export function emptyCellSelection(view: EditorView): WidgetSelection | null {
	const cell = emptyOpenCell(view.state);
	const widget = cell && drawnTableAt(view, cell.tableFrom);
	const td = widget && cellElement(widget, cell);
	if (!widget || !td) return null;
	// The same range a tap on an empty cell selects: the cell's contents, which for an empty cell is a caret at its start.
	const range = document.createRange();
	range.selectNodeContents(td);
	if (!range.collapsed) range.collapse(true);
	const raised = selectionIn(view, widget, range);
	if (!raised) return null;
	// That caret's own box has no size, so the offer is placed on the cell's box, the one its open editor is laid over. An empty
	// cell holds no text for it to cover: it sits inside the cell, at its far end, away from the caret at the start.
	// A cell too narrow to hold it (a phone's last column) puts it beside or below the table, as the offer for selected text goes.
	const box = td.getBoundingClientRect();
	if (box.width < OFFER_ROOM) {
		const beside = offerBesideTable(view, cell.tableFrom, { top: box.top, bottom: box.bottom });
		if (beside) return { ...raised, above: false, ...beside };
	}
	return { ...raised, above: false, exact: true, end: true, top: (box.top + box.bottom) / 2, left: box.right - OFFER_GAP };
}

/** The widest the Comment offer is (on a phone), and the gap kept around it. */
const OFFER_GAP = 6;
// Under the table the offer has the table's own bottom margin to fit in, about the offer's height and a few pixels more.
const BELOW_GAP = 3;
const OFFER_ROOM = 120 + 2 * OFFER_GAP;

/**
 * Where the Comment offer for text selected in a table cell goes so that it covers no text: in the margin beside the table,
 * level with the selected line, and where the table leaves no margin directly below it.
 */
export function offerBesideTable(view: EditorView, from: number, line: Extent) {
	const widget = drawnTableAt(view, from);
	if (!widget) return null;
	// The table's own box: the widget around it carries padding the offer must not sit in, with the text below.
	const table = (widget.querySelector('table') ?? widget).getBoundingClientRect(), column = view.scrollDOM.getBoundingClientRect();
	if (column.right - table.right >= OFFER_ROOM) return { exact: true, top: (line.top + line.bottom) / 2, left: table.right + OFFER_GAP };
	return { exact: true, below: true, top: table.bottom + BELOW_GAP, left: table.left };
}

/**
 * The open cell's own editor and where its text starts in the document, when `from`..`to` lies in that cell; null otherwise.
 * The table hides the cell's source in the document, so its text is measured in that editor, as prose is in the document's.
 */
export function openCellText(view: EditorView, from: number, to: number): { inner: EditorView; base: number } | null {
	const cell = view.state.field(openCellState, false);
	if (!cell || from < cell.from || to > cell.to) return null;
	const el = view.dom.querySelector<HTMLElement>(`${CELL_EDITOR} .cm-editor`);
	const inner = el && EditorView.findFromDOM(el);
	return inner ? { inner, base: cell.from } : null;
}

type Extent = { top: number; bottom: number };
/** The vertical extent of `boxes`, or null when there are none. */
const extent = (boxes: Extent[]): Extent | null =>
	boxes.length ? { top: Math.min(...boxes.map((b) => b.top)), bottom: Math.max(...boxes.map((b) => b.bottom)) } : null;

/**
 * The box a comment's composer is anchored to for a range inside one table cell, as for prose (`rowsOf`): the lines its text
 * is drawn on, out to the text column's edge or the table's, whichever is further. Its lines are read off the open cell's
 * editor; once the cell closes, off the draft's mark drawn in the cell; failing both, the cell. Null outside a table cell.
 */
export function cellTextRows(view: EditorView, from: number, to: number): DOMRect | null {
	const widget = drawnTableAt(view, from), block = widget && blockRange(view, widget);
	if (!widget || !block || to > block.to) return null;
	const cell = tableCells(view.state.sliceDoc(block.from, block.to))
		.find((s) => from - block.from >= s.rawFrom && to - block.from <= s.rawTo);
	const td = cell && cellElement(widget, cell);
	if (!td) return null;
	const open = openCellText(view, from, to);
	const ends = open ? [open.inner.coordsAtPos(from - open.base, 1), open.inner.coordsAtPos(to - open.base, 1)] : [];
	const drawn = [...(td.matches(PENDING) ? [td] : []), ...td.querySelectorAll<HTMLElement>(PENDING)].flatMap((el) => [...el.getClientRects()]);
	const lines = (ends.every(Boolean) ? extent(ends as Extent[]) : null) ?? extent(drawn) ?? extent([td.getBoundingClientRect()])!;
	return acrossColumn(view, lines.top, lines.bottom, widget.getBoundingClientRect().right);
}
