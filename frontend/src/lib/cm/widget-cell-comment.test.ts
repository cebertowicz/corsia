import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { commentRanges } from './comment-marks';
import { pendingCommentMark } from './pending-comment-mark';
import { widgetComments, type WidgetSelection } from './widget-comments';
import { cellRowsAt, emptyCellSelection, openCellPlace } from './widget-cell-comment';
import { type Rig } from './typed-reveal-rig';
import { build, cellEditor, cellEl, cleanup, clicked, deleted, typed } from './widget-cell-rig';

afterEach(() => { vi.restoreAllMocks(); });

/**
 * An empty cell has no text to select for a comment, so while its editor is open and still empty the Comment offer is
 * the whole cell's. The first character typed takes the offer away, and emptying the cell again brings it back.
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
	it('is raised for the whole cell when the editor opens empty', () => {
		const r = openCell(1);
		expect(raised.at(-1), 'the offer is up as soon as the empty cell opens').toEqual(wholeEmptyCell());
		const offer = raised.at(-1)!;
		expect(offer.from >= rowFrom && offer.to <= rowTo, 'anchored on that cell, in its own row').toBe(true);
		expect(emptyCellSelection(r.view), 'the one answer the editor and the widget flows both read').toMatchObject({ from: offer.from, to: offer.to });
	});

	it('is placed on the open cell\'s own box, not on its empty caret', () => {
		const r = openCell(1);
		vi.spyOn(cellEl(r, 1, 1), 'getBoundingClientRect').mockReturnValue(new DOMRect(312, 140, 120, 36));
		expect(emptyCellSelection(r.view), 'above the cell\'s top edge, from its left').toMatchObject({ top: 140, left: 312, above: true });
	});

	it('goes when the first character is typed', () => {
		const r = openCell(1);
		typed(r, 1, 'x');
		vi.advanceTimersByTime(50);
		expect(raised.at(-1), 'typing takes the offer away').toBeNull();
		expect(emptyCellSelection(r.view), 'a cell with text has no whole-cell offer').toBeNull();
	});

	it('comes back when everything in the cell is deleted', () => {
		const r = openCell(1);
		typed(r, 1, 'x');
		vi.advanceTimersByTime(50);
		expect(raised.at(-1)).toBeNull();
		deleted(r, 1, 2);
		vi.advanceTimersByTime(50);
		expect(raised.at(-1), 'the cell is empty again, so the offer is back').toEqual(wholeEmptyCell());
	});

	it('is not raised when the cell opened has text', () => {
		const r = openCell(0);
		expect(raised.filter(Boolean), 'a caret in text raises nothing').toEqual([]);
		expect(emptyCellSelection(r.view)).toBeNull();
	});
});

// 'Log' in the open cell ` Login `, as document offsets.
const logFrom = rowFrom + 2, logTo = logFrom + 3;

describe('text selected in an open cell', () => {
	it('places the offer off the selected text in the cell editor, as a drawn cell\'s selection is', () => {
		const r = openCell(0);
		// Two drawn lines: the offer sits above the first and from where the last ends.
		vi.spyOn(Range.prototype, 'getClientRects').mockImplementation(function (this: Range) {
			return (this.toString() === 'Log' ? [new DOMRect(300, 120, 20, 16), new DOMRect(290, 138, 12, 16)] : []) as unknown as DOMRectList;
		});
		expect(openCellPlace(r.view, logFrom, logTo), 'measured off the selected words').toEqual({ top: 120, left: 302 });
	});

	it('places nothing for a range outside the open cell, or with no cell open', () => {
		const r = openCell(0);
		expect(openCellPlace(r.view, 0, 3), 'prose is placed by the editor').toBeNull();
		const closed = build(DOC);
		rigs.push(closed);
		expect(openCellPlace(closed.view, logFrom, logTo)).toBeNull();
	});

	it('anchors the composer to the cell\'s row across the table, open or not, and to nothing outside a table', () => {
		const r = openCell(0);
		vi.spyOn(cellEl(r, 1, 0).parentElement!, 'getBoundingClientRect').mockReturnValue(new DOMRect(40, 200, 300, 32));
		vi.spyOn(r.view.dom.querySelector<HTMLElement>('.cm-md-table')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(36, 150, 320, 90));
		expect(cellRowsAt(r.view, logFrom, logTo), 'the row, as wide as the table').toEqual(new DOMRect(36, 200, 320, 32));
		expect(cellRowsAt(r.view, 0, 3), 'prose keeps its own rows').toBeNull();
		const closed = build(DOC);
		rigs.push(closed);
		vi.spyOn(cellEl(closed, 1, 0).parentElement!, 'getBoundingClientRect').mockReturnValue(new DOMRect(40, 200, 300, 32));
		vi.spyOn(closed.view.dom.querySelector<HTMLElement>('.cm-md-table')!, 'getBoundingClientRect').mockReturnValue(new DOMRect(36, 150, 320, 90));
		expect(cellRowsAt(closed.view, logFrom, logTo), 'the same row once the cell closes').toEqual(new DOMRect(36, 200, 320, 32));
	});
});
