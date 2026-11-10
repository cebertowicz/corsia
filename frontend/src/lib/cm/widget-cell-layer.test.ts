import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { type Rig } from './typed-reveal-rig';
import { commentRanges, setCommentRanges } from './comment-marks';
import { pendingCommentMark } from './pending-comment-mark';
import { widgetComments } from './widget-comments';
import { cellEl, cellText, cleanup, clicked, DOC, editingCell, layer, mountedEditor, open, press, typed } from './widget-cell-rig';

/**
 * Where a cell's editor is allowed to live. Chromium places no caret in a contenteditable element inside CodeMirror's
 * editing host, and a drawn table cell is inside it, so the editor is held in a layer outside that host and kept on the
 * cell's box, while the cell keeps its own live text unseen underneath so the row keeps the height that text gives it.
 * jsdom lays nothing out, so the cell's box is stubbed where the layer copies it.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

/** The cell's box, since jsdom gives every element a zero one. */
const boxOf = (element: Element, left: number, top: number, width: number, height: number) => {
	element.getBoundingClientRect = () => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) } as DOMRect);
};

describe('the layer a cell editor lives in', () => {
	it('holds the editor outside the cell it edits, in the editor\'s own box, and takes the cell\'s box', () => {
		const r = track(open());
		const cell = cellEl(r);
		boxOf(cell, 320, 140, 210, 44);
		clicked(r);
		const held = layer(r), editor = mountedEditor(r);
		expect(held, 'the layer opened').not.toBeNull();
		expect(editor, 'and the cell editor in it').not.toBeNull();
		expect(held!.contains(editor!), 'the editor is in the layer').toBe(true);
		expect(cell.contains(editor!), 'and not inside the cell, where no caret could go').toBe(false);
		expect(r.view.dom.contains(held!), 'the layer is in the editor, so the cell\'s type and theme reach it').toBe(true);
		expect(held!.style.left, 'the cell\'s box, copied').toBe('320px');
		expect(held!.style.top).toBe('140px');
		expect(held!.style.width).toBe('210px');
		expect(held!.style.minHeight, 'never shorter than the cell it covers').toBe('44px');
		expect(editingCell(r), 'the cell it covers is the one clicked').toBe(cell);
	});

	it('follows the cell however the layout moves it and lets go when the cell closes', () => {
		const r = track(open());
		const cell = cellEl(r);
		boxOf(cell, 320, 140, 210, 44);
		clicked(r);
		// A resize, a scroll, and the whole editor moving down the page under a notice drawn above it all move the cell's box,
		// and only the first two announce themselves; the layer is on the cell's box by the next frame either way.
		boxOf(cell, 120, 300, 480, 66);
		vi.advanceTimersByTime(20);
		expect(layer(r)!.style.left, 'the layer moves with the cell').toBe('120px');
		expect(layer(r)!.style.width).toBe('480px');
		boxOf(cell, 120, 408, 480, 66);
		vi.advanceTimersByTime(20);
		expect(layer(r)!.style.top, 'with nothing announcing the move').toBe('408px');
		expect(layer(r)!.style.minHeight).toBe('66px');
		press(r, 'Escape');
		expect(layer(r), 'closing takes the layer out of the document').toBeNull();
		expect(editingCell(r), 'and the cell is a plain rendered cell again').toBeNull();
	});

	it('leaves the cell\'s own live text under the editor, so the row grows with what is typed', () => {
		const r = track(open());
		clicked(r);
		typed(r, 6, ' and a great many words after them');
		expect(cellText(r), 'the editor holds the cell\'s source').toBe(' Login and a great many words after them ');
		expect(editingCell(r)!.textContent, 'the cell underneath shows the same text, unseen').toBe('Login and a great many words after them');
	});

	it('gives a closed cell its comment back, drawn on the same words', async () => {
		const r = track(open([commentRanges, pendingCommentMark, widgetComments({ onSelect: vi.fn(), onFocus: vi.fn() })]));
		const from = DOC.indexOf('Login');
		r.view.dispatch({ effects: setCommentRanges.of([{ id: 'c1', from, to: from + 3 }]) });
		// The paint pass runs in CodeMirror's measure frame, and again when the table's nodes change.
		const settled = async () => { for (let pass = 0; pass < 3; pass++) { await Promise.resolve(); vi.advanceTimersByTime(50); } };
		await settled();
		const anchor = () => cellEl(r).querySelector('[data-comment-ids="c1"]')?.textContent;
		expect(anchor(), 'setup: the comment is drawn on its words').toBe('Log');
		clicked(r);
		press(r, 'Escape');
		expect(editingCell(r), 'the cell closed').toBeNull();
		await settled();
		expect(anchor(), 'and its comment is drawn on the same words again').toBe('Log');
	});
});
