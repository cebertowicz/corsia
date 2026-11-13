import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { type Rig } from './typed-reveal-rig';
import { commentRanges, setCommentRanges } from './comment-marks';
import { pendingCommentMark } from './pending-comment-mark';
import { widgetComments } from './widget-comments';
import { cellEditor, cellEl, cleanup, clicked, DOC, editingCell, open, press, typed } from './widget-cell-rig';

/**
 * A comment drawn on a cell's words while the cell is closed and while it is open. Open, the cell's content is its editor,
 * so the paint pass leaves that editor's DOM alone and the editor draws the mark itself, with the same classes, on the same words.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

// The paint pass runs in CodeMirror's measure frame, and again when the table's nodes change.
const settled = async () => { for (let pass = 0; pass < 3; pass++) { await Promise.resolve(); vi.advanceTimersByTime(50); } };

function commented() {
	const r = track(open([commentRanges, pendingCommentMark, widgetComments({ onSelect: vi.fn(), onFocus: vi.fn() })]));
	const from = DOC.indexOf('Login');
	r.view.dispatch({ effects: setCommentRanges.of([{ id: 'c1', from, to: from + 3 }]) });
	return r;
}
const drawnOn = (el: Element | null | undefined) => el?.querySelector('[data-comment-ids="c1"]')?.textContent;

describe('a comment on a cell\'s words', () => {
	it('is drawn by the open cell\'s editor on the same words, and painted into none of its DOM', async () => {
		const r = commented();
		await settled();
		expect(drawnOn(cellEl(r)), 'setup: the comment is drawn on its words').toBe('Log');
		clicked(r);
		await settled();
		const editor = cellEditor(r)!;
		expect(editingCell(r)?.contains(editor.dom), 'the editor is in the cell').toBe(true);
		const mark = editor.contentDOM.querySelector('.cm-comment-anchor[data-comment-ids="c1"]');
		expect(mark?.textContent, 'the editor draws the comment on the same words').toBe('Log');
		expect(editor.contentDOM.querySelector('.cm-comment-wrap'), 'the paint pass wrapped nothing in the editor').toBeNull();
	});

	it('follows the words as the open cell is typed in', async () => {
		const r = commented();
		clicked(r);
		await settled();
		typed(r, 0, 'My ');
		await settled();
		expect(cellEditor(r)!.contentDOM.querySelector('[data-comment-ids="c1"]')?.textContent).toBe('Log');
	});

	// A phone holds Enter back for a frame and drops it if any transaction reaches the editor first (CodeMirror's Android
	// key handling), so the paint pass that follows a typed character must send the cell's editor nothing it already has.
	it.each([
		['with no comment in the cell', false],
		['with a comment the typing moves along', true]
	])('sends the open cell\'s editor nothing after a keystroke that left its marks as they were, %s', async (_name, withComment) => {
		const r = withComment ? commented() : track(open([commentRanges, pendingCommentMark, widgetComments({ onSelect: vi.fn(), onFocus: vi.fn() })]));
		clicked(r);
		await settled();
		const editor = cellEditor(r)!;
		typed(r, 0, 'My ');
		const dispatch = vi.spyOn(editor, 'dispatch');
		await settled();
		expect(dispatch, 'the paint pass after the keystroke dispatched nothing into the cell').not.toHaveBeenCalled();
		if (withComment) expect(editor.contentDOM.querySelector('[data-comment-ids="c1"]')?.textContent, 'and the mark is still on its words').toBe('Log');
	});

	it('is drawn on the same words again when the cell closes', async () => {
		const r = commented();
		await settled();
		clicked(r);
		press(r, 'Escape');
		expect(editingCell(r), 'the cell closed').toBeNull();
		await settled();
		expect(drawnOn(cellEl(r)), 'and its comment is drawn on the same words again').toBe('Log');
	});
});
