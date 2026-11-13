import { StateEffect, StateField, type EditorState, type Range } from '@codemirror/state';
import { Decoration, EditorView, WidgetType, type DecorationSet } from '@codemirror/view';
import { blockRange, cellElement, drawnTableAt } from './widget-blocks';
import { CELL_EDITOR, cellSource, openCellState } from './widget-cell-state';
import { tableCells } from './widget-cells';
import { heldText, heldToSource, sourceToHeld } from './widget-cell-row';
import { cellTextRange, markClasses, suggestionInsert, type CellSuggestion, type Mark } from './widget-paint';
import { colourStyle } from './suggestion-marks';
import { OFFER_GAP, OFFER_ROOM, offerPlace } from './offer-place';
import { selectionIn } from './widget-selection';
import { acrossColumn } from './comment-anchor-geometry';
import type { WidgetSelection } from './widget-selection-types';

/**
 * The comment and suggestion marks inside the open cell's text, as that editor's own decorations: the cell's drawn content is
 * the editor while it is open, so the paint pass (`widget-paint.ts`) leaves it alone and writes them here instead, with the
 * same classes it paints a closed cell with. Between paints they follow the cell's typing.
 */
const setCellMarks = StateEffect.define<DecorationSet>();
export const cellMarksField = StateField.define<DecorationSet>({
	create: () => Decoration.none,
	update: (marks, tr) => tr.effects.find((effect) => effect.is(setCellMarks))?.value ?? marks.map(tr.changes),
	provide: (field) => EditorView.decorations.from(field)
});

class SuggestionInsert extends WidgetType {
	constructor(readonly mark: Mark, readonly suggestion: CellSuggestion) { super(); }
	eq(other: SuggestionInsert) { return JSON.stringify([this.mark, this.suggestion]) === JSON.stringify([other.mark, other.suggestion]); }
	toDOM() { return suggestionInsert(this.mark, this.suggestion); }
}

/** A decoration set as its ranges, in order, for comparing what an editor holds with what it should. */
const rangesOf = (set: DecorationSet) => {
	const out: [number, number, unknown][] = [];
	for (const cursor = set.iter(); cursor.value; cursor.next()) out.push([cursor.from, cursor.to, cursor.value.spec]);
	return JSON.stringify(out);
};

/**
 * Writes the marks of `marks` that lie inside the open cell's text into its editor; a mark over the whole cell, or across
 * cells, stays the cell's own (`paintTable`). Only a change is sent: the marks already follow typing, and a transaction the
 * editor does not need drops the Enter a phone holds back for a frame (CodeMirror's Android key handling).
 */
export function paintOpenCell(view: EditorView, marks: Mark[]) {
	const cell = view.state.field(openCellState, false);
	const el = cell && view.dom.querySelector<HTMLElement>(`${CELL_EDITOR} .cm-editor`);
	const inner = el && EditorView.findFromDOM(el);
	if (!cell || !inner) return;
	const source = cellSource(view.state, cell), held = heldText(source, inner.state.doc.toString());
	const start = cell.from + held.base, end = cell.from + heldToSource(held, held.text.length);
	const at = (pos: number) => sourceToHeld(source, held, pos - cell.from);
	const ranges: Range<Decoration>[] = [];
	for (const mark of marks) {
		if (mark.from < cell.from || mark.to > cell.to || (mark.from <= start && mark.to >= end && !mark.suggestion)) continue;
		const from = at(mark.from), to = at(mark.to);
		if (from >= to) continue;
		const suggestion = mark.suggestion;
		const attributes: Record<string, string> = suggestion
			? { class: ['cm-suggestion-del', ...(mark.kind === 'focused' ? ['cm-suggestion-focus'] : [])].join(' '), style: colourStyle(suggestion.color), 'data-suggestion-id': mark.id }
			: { class: markClasses(mark.kind).join(' '), ...(mark.kind === 'pending' ? {} : { 'data-comment-ids': mark.id }) };
		ranges.push(Decoration.mark({ attributes }).range(from, to));
		if (suggestion) ranges.push(Decoration.widget({ widget: new SuggestionInsert(mark, suggestion), side: 1 }).range(to));
	}
	const next = Decoration.set(ranges, true);
	if (rangesOf(inner.state.field(cellMarksField)) === rangesOf(next)) return;
	inner.dispatch({ effects: setCellMarks.of(next) });
}

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
 * The whole-cell selection the Comment offer is raised for while the open cell is empty, placed on that cell: centred vertically
 * in it, with its right end at the cell's right edge (beside or below the table when the cell is too narrow). Null otherwise.
 * Its one writer is the editor's own selection flow (`selectionOffer`), which raises it for a
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
	if (box.width < OFFER_ROOM) return { ...raised, above: false, ...offerPlace(view, cell.tableFrom, box) };
	return { ...raised, above: false, exact: true, end: true, top: (box.top + box.bottom) / 2, left: box.right - OFFER_GAP };
}

/**
 * The open cell's own editor and where a document position in the cell sits in its content, when `from`..`to` lies in that
 * cell; null otherwise. The table hides the cell's source in the document, so its text is measured in that editor, as prose
 * is in the document's.
 */
export function openCellText(view: EditorView, from: number, to: number): { inner: EditorView; at: (pos: number) => number } | null {
	const cell = view.state.field(openCellState, false);
	if (!cell || from < cell.from || to > cell.to) return null;
	const el = view.dom.querySelector<HTMLElement>(`${CELL_EDITOR} .cm-editor`);
	const inner = el && EditorView.findFromDOM(el);
	if (!inner) return null;
	const source = cellSource(view.state, cell), held = heldText(source, inner.state.doc.toString());
	return { inner, at: (pos) => sourceToHeld(source, held, pos - cell.from) };
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
	const ends = open ? [open.inner.coordsAtPos(open.at(from), 1), open.inner.coordsAtPos(open.at(to), 1)] : [];
	const drawn = open ? null : cellTextRange(td, view.state.sliceDoc(block.from, block.to), cell, from - block.from, to - block.from);
	const lines = (ends.every(Boolean) ? extent(ends as Extent[]) : null) ?? extent(drawn ? [...drawn.getClientRects()] : []) ?? extent([td.getBoundingClientRect()])!;
	return acrossColumn(view, lines.top, lines.bottom, widget.getBoundingClientRect().right);
}
