import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { cellClicked } from './widget-cell-click';
import { type Rig } from './typed-reveal-rig';
import { cellEl, cleanup, open } from './widget-cell-rig';

/**
 * Who a click or tap on a drawn cell belongs to. In a document that can be edited it opens the cell's editor, and the
 * comment flows leave it alone; in a read-only one nothing opens, so the comment flows may take it as a selection.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; document.getSelection()?.removeAllRanges(); });

describe('cellClicked', () => {
	it('gives a click on a cell of an editable document to the cell editor', () => {
		const r = track(open());
		const clicked = cellClicked(r.view, cellEl(r));
		expect(clicked?.cell, 'the cell clicked is the one that opens').toBe(cellEl(r));
		expect(clicked?.row.rowIndex).toBe(1);
	});

	it('gives nothing to the cell editor in a read-only document, so a tap there can still select the cell', () => {
		const r = track(open([EditorState.readOnly.of(true)]));
		expect(cellClicked(r.view, cellEl(r))).toBeNull();
	});

	it('leaves a link inside a cell to its own click', () => {
		const r = track(open());
		const link = document.createElement('a');
		cellEl(r).append(link);
		expect(cellClicked(r.view, link)).toBeNull();
	});

	it('opens a cell from a click on a comment drawn on some of its words, which opens the comment too', () => {
		const r = track(open());
		const marked = document.createElement('span');
		marked.dataset.commentIds = 'c1';
		cellEl(r).append(marked);
		expect(cellClicked(r.view, marked)?.cell).toBe(cellEl(r));
	});

	it('opens a cell that a comment covers whole, on the cell or on the whole table', () => {
		for (const [name, mark] of [['the cell', (r: Rig) => cellEl(r)], ['the table widget', (r: Rig) => cellEl(r).closest('.cm-md-table') as HTMLElement]] as const) {
			const r = track(open());
			mark(r).dataset.commentIds = 'c1';
			const clicked = cellClicked(r.view, cellEl(r));
			expect(clicked?.cell, `a comment on ${name} does not stop its cell opening`).toBe(cellEl(r));
		}
	});

	it('gives a press on the table\'s padding or a border to the nearest cell', () => {
		const r = track(open());
		const widget = cellEl(r).closest<HTMLElement>('.cm-md-table')!;
		const boxes = [[0, 0], [1, 0], [0, 1], [1, 1]].map(([row, col]) => ({ row, col, x: 40 + col * 100, y: 100 + row * 40 }));
		for (const { row, col, x, y } of boxes) vi.spyOn(cellEl(r, row, col), 'getBoundingClientRect').mockReturnValue(new DOMRect(x, y, 100, 40));
		// 6px under the table's last row, below its right-hand cell, and 3px left of it, level with its first row.
		expect(cellClicked(r.view, widget, { x: 160, y: 186 })?.cell, 'below the body row, nearest its second cell').toBe(cellEl(r, 1, 1));
		expect(cellClicked(r.view, widget, { x: 37, y: 110 })?.cell, 'left of the header row, nearest its first cell').toBe(cellEl(r, 0, 0));
		expect(cellClicked(r.view, widget), 'with no point to go by, the padding is no cell\'s').toBeNull();
	});

	it('leaves a selection already dragged inside the table to the comment flows', () => {
		const r = track(open());
		const range = document.createRange();
		range.selectNodeContents(cellEl(r));
		document.getSelection()!.addRange(range);
		expect(cellClicked(r.view, cellEl(r))).toBeNull();
	});
});
