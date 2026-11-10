import { afterEach, describe, expect, it } from 'vitest';
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

	it('leaves a link or a drawn comment inside a cell to its own click', () => {
		const r = track(open());
		const marked = document.createElement('span');
		marked.dataset.commentIds = 'c1';
		cellEl(r).append(marked);
		expect(cellClicked(r.view, marked)).toBeNull();
	});

	it('leaves a selection already dragged inside the table to the comment flows', () => {
		const r = track(open());
		const range = document.createRange();
		range.selectNodeContents(cellEl(r));
		document.getSelection()!.addRange(range);
		expect(cellClicked(r.view, cellEl(r))).toBeNull();
	});
});
