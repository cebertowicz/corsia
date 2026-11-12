import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { commentRanges } from './comment-marks';
import { pendingCommentMark } from './pending-comment-mark';
import { widgetComments, type WidgetSelection } from './widget-comments';
import { cellTextRows, emptyCellSelection, openCellText } from './widget-cell-comment';
import { selectionOffer } from './selection-offer';
import { offerPlace } from './offer-place';
import { type Rig } from './typed-reveal-rig';
import { build, cellEditor, cellEl, cleanup, clicked, deleted, typed } from './widget-cell-rig';

afterEach(() => { vi.restoreAllMocks(); });

/**
 * An empty cell has no text to select for a comment, so while its editor is open and still empty the Comment offer is
 * the whole cell's. The editor's own selection flow raises and clears it (`MarkdownEditor.emptyCell.test.ts`); these specs
 * place it, and check the widget comment flow leaves it alone.
 */

const ROW = '| Login |  |';
const DOC = `# Plan\n\n| Feature | Status |\n| --- | --- |\n${ROW}\n\nafter\n`;
const rowFrom = DOC.indexOf(ROW), rowTo = rowFrom + ROW.length;

const rigs: Rig[] = [];
let raised: Array<WidgetSelection | null> = [];

beforeEach(() => { vi.useFakeTimers(); raised = []; });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

function openCell(col: number) {
	const r = build(DOC, [commentRanges, pendingCommentMark, widgetComments({ onSelect: (selection) => raised.push(selection), onFocus: vi.fn() })]);
	rigs.push(r);
	clicked(r, cellEl(r, 1, col));
	expect(cellEditor(r), 'setup: the cell editor opened').not.toBeNull();
	vi.advanceTimersByTime(50);
	return r;
}

const wholeEmptyCell = () => expect.objectContaining({ inWidget: true, from: expect.any(Number), to: expect.any(Number) });

describe('the Comment offer on an empty cell being edited', () => {
	it('is the whole cell, anchored in its own row, and the widget comment flow raises and clears none of it', () => {
		const r = openCell(1);
		const offer = emptyCellSelection(r.view);
		expect(offer, 'the whole cell is offered').toEqual(wholeEmptyCell());
		expect(offer!.from >= rowFrom && offer!.to <= rowTo, 'anchored on that cell, in its own row').toBe(true);
		typed(r, 1, 'x');
		vi.advanceTimersByTime(50);
		deleted(r, 1, 2);
		vi.advanceTimersByTime(50);
		expect(raised, 'the editor\'s own selection flow is the one writer').toEqual([]);
	});

	it("is placed inside the open cell's own box, at its far end, so it covers no text", () => {
		const r = openCell(1);
		vi.spyOn(cellEl(r, 1, 1), 'getBoundingClientRect').mockReturnValue(new DOMRect(312, 140, 200, 36));
		const offer = emptyCellSelection(r.view);
		expect(offer, "level with the cell's middle, ending just inside its right edge").toMatchObject({ top: 158, left: 506, exact: true, end: true, above: false });
	});

	it('leaves a cell too narrow to hold it for beside or below the table', () => {
		const r = openCell(1);
		vi.spyOn(cellEl(r, 1, 1), 'getBoundingClientRect').mockReturnValue(new DOMRect(312, 140, 100, 36));
		const offer = emptyCellSelection(r.view);
		expect(offer, 'not placed at the narrow cell\'s own far end').not.toMatchObject({ end: true });
		expect(offer).toMatchObject({ exact: true, above: false });
	});

	it('is none once the cell has text, and none for a cell opened with text', () => {
		const r = openCell(1);
		typed(r, 1, 'x');
		expect(emptyCellSelection(r.view), 'a cell with text has no whole-cell offer').toBeNull();
		deleted(r, 1, 2);
		expect(emptyCellSelection(r.view), 'emptied, it has one again').toEqual(wholeEmptyCell());
		expect(emptyCellSelection(openCell(0).view)).toBeNull();
	});
});

// 'Log' in the open cell ` Login `, as document offsets.
const logFrom = rowFrom + 2, logTo = logFrom + 3;

describe('text selected in an open cell', () => {
	it('is measured in the cell\'s own editor, from where the cell starts in the document', () => {
		const r = openCell(0);
		const open = openCellText(r.view, logFrom, logTo);
		expect(open?.inner.dom.closest('.cm-md-cell-editor'), 'the open cell\'s editor').not.toBeNull();
		expect(open!.inner.state.sliceDoc(open!.at(logFrom), open!.at(logTo)), 'the same words').toBe('Log');
	});

	it('is not measured there for a range outside the open cell, or with no cell open', () => {
		const r = openCell(0);
		expect(openCellText(r.view, 0, 3), 'prose is measured by the document\'s editor').toBeNull();
		const closed = build(DOC);
		rigs.push(closed);
		expect(openCellText(closed.view, logFrom, logTo)).toBeNull();
	});

	it("places the Comment offer in the margin beside the table, level with the selected line, measured in the cell's editor", () => {
		const r = openCell(0);
		r.view.dispatch({ selection: { anchor: logFrom, head: logTo } });
		const { inner, at } = openCellText(r.view, logFrom, logTo)!;
		vi.spyOn(inner, 'coordsAtPos').mockImplementation((pos) => pos === at(logTo) ? { left: 302, right: 302, top: 138, bottom: 154 } : null);
		vi.spyOn(r.view.dom.querySelector<HTMLElement>('.cm-md-table table')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(36, 120, 320, 90));
		vi.spyOn(r.view.scrollDOM, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 700, 900));
		const offer = selectionOffer(r.view, () => () => null);
		expect(offer, 'right of the table, on the selected line').toMatchObject({ from: logFrom, to: logTo, text: 'Log', top: 146, left: 362, exact: true });
		expect(offer?.below, 'not under the table').toBeUndefined();
	});

	it('places it directly below the table where the table leaves no margin', () => {
		const r = openCell(0);
		r.view.dispatch({ selection: { anchor: logFrom, head: logTo } });
		const { inner, at } = openCellText(r.view, logFrom, logTo)!;
		vi.spyOn(inner, 'coordsAtPos').mockImplementation((pos) => pos === at(logTo) ? { left: 302, right: 302, top: 138, bottom: 154 } : null);
		vi.spyOn(r.view.dom.querySelector<HTMLElement>('.cm-md-table table')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(16, 120, 340, 90));
		vi.spyOn(r.view.scrollDOM, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 372, 900));
		expect(selectionOffer(r.view, () => () => null), 'under the table, from its left').toMatchObject({ top: 213, left: 16, exact: true, below: true });
	});

	it('(d) is placed by the one rule prose follows: beside where the words end, which in a table is the margin beside it', () => {
		const r = openCell(0);
		vi.spyOn(r.view.dom.querySelector<HTMLElement>('.cm-md-table table')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(36, 120, 320, 90));
		vi.spyOn(r.view.scrollDOM, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 700, 900));
		const words = { top: 138, bottom: 154, right: 302 };
		expect(offerPlace(r.view, 2, words), 'prose: beside the end of the words').toEqual({ top: 138, left: 302 });
		expect(offerPlace(r.view, logFrom, words), 'a cell: the same rule, the margin beside the table').toEqual({ exact: true, top: 146, left: 362 });
		r.view.dispatch({ selection: { anchor: logFrom, head: logTo } });
		const { inner, at } = openCellText(r.view, logFrom, logTo)!;
		vi.spyOn(inner, 'coordsAtPos').mockImplementation((pos) => pos === at(logTo) ? { left: 302, right: 302, top: 138, bottom: 154 } : null);
		expect(selectionOffer(r.view, () => () => null), 'the open cell\'s offer is that place').toMatchObject(offerPlace(r.view, logFrom, words));
		r.view.dispatch({ selection: { anchor: 2, head: 6 } });
		vi.spyOn(r.view, 'coordsAtPos').mockReturnValue({ left: 90, right: 90, top: 20, bottom: 36 });
		expect(selectionOffer(r.view, () => () => null), 'and prose\'s offer is its place').toMatchObject(offerPlace(r.view, 2, { top: 20, bottom: 36, right: 90 }));
	});

	it('measures text after an escaped pipe at its place in the cell\'s content, which has no escape', () => {
		const piped = DOC.replace('| Login |', '| a\\|Login |');
		const r = build(piped, [commentRanges, pendingCommentMark, widgetComments({ onSelect: vi.fn(), onFocus: vi.fn() })]);
		rigs.push(r);
		clicked(r, cellEl(r, 1, 0));
		const from = piped.indexOf('Login'), to = from + 3;
		const open = openCellText(r.view, from, to)!;
		expect(open.inner.state.doc.toString(), 'the editor holds the content').toBe(' a|Login ');
		expect(open.inner.state.sliceDoc(open.at(from), open.at(to))).toBe('Log');
	});

	/** The document's text column and the table, as laid out: the composer's box runs from the column's left to the table's right. */
	const layOut = (rig: Rig) => {
		vi.spyOn(rig.view.contentDOM, 'getBoundingClientRect').mockReturnValue(new DOMRect(20, 0, 600, 900));
		vi.spyOn(rig.view.dom.querySelector<HTMLElement>('.cm-md-table')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(36, 150, 320, 90));
		vi.spyOn(cellEl(rig, 1, 0), 'getBoundingClientRect').mockReturnValue(new DOMRect(40, 200, 100, 48));
	};

	it('anchors the composer, as prose, to the lines the words are drawn on in the open cell', () => {
		const r = openCell(0);
		layOut(r);
		const { inner, at } = openCellText(r.view, logFrom, logTo)!;
		// The words wrap: they start on one line of the cell and end on the next.
		vi.spyOn(inner, 'coordsAtPos').mockImplementation((pos) => pos === at(logFrom) ? { left: 48, right: 48, top: 204, bottom: 220 } : { left: 60, right: 60, top: 222, bottom: 238 });
		expect(cellTextRows(r.view, logFrom, logTo), 'those lines, from the column\'s left to the table\'s right').toEqual(new DOMRect(20, 204, 336, 34));
		expect(cellTextRows(r.view, 0, 3), 'prose keeps its own rows').toBeNull();
	});

	it('once the cell closes, anchors to the draft drawn in the cell, and failing that to the cell', () => {
		const closed = build(DOC);
		rigs.push(closed);
		layOut(closed);
		expect(cellTextRows(closed.view, logFrom, logTo), 'no draft drawn: the cell\'s own lines').toEqual(new DOMRect(20, 200, 336, 48));
		const drawn = document.createElement('span');
		drawn.className = 'cm-comment-pending';
		cellEl(closed, 1, 0).append(drawn);
		vi.spyOn(drawn, 'getClientRects').mockReturnValue([new DOMRect(44, 206, 30, 16)] as unknown as DOMRectList);
		expect(cellTextRows(closed.view, logFrom, logTo), 'the line the draft is drawn on').toEqual(new DOMRect(20, 206, 336, 16));
	});
});
