import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { forceParsing } from '@codemirror/language';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import { commentRanges, setCommentRanges, type CommentRange } from './comment-marks';
import { markdownLivePreview } from './markdown-live';
import { pendingCommentMark } from './pending-comment-mark';
import { selectionIn, widgetComments, type WidgetSelection } from './widget-comments';

const TABLE = '| Item | Note |\n|---|---|\n| Alpha | ab ab |\n| Bravo | Eli |\n| Charlie ||';
const DOC = `intro\n\n${TABLE}\n\n\`\`\`mermaid\ngraph TD; A-->B\n\`\`\`\n`;
const TABLE_FROM = DOC.indexOf('| Item');
const FENCE_FROM = DOC.indexOf('```mermaid');
const FENCE_TO = DOC.length - 1;

const rect = (top: number) => ({ top, height: 40, bottom: top + 40, left: 0, right: 100, width: 100, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
const label = (selection: WidgetSelection | null) => typeof selection?.label === 'function' ? selection.label() : selection?.label;

/** The first parse stops after 20ms of wall time and finishes on a timer these tests never run, so parse the document now. */
function parsed(view: EditorView) {
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	return view;
}

const open = (extra: Extension = []) =>
	parsed(new EditorView({ state: EditorState.create({ doc: DOC, extensions: [markdown({ extensions: GFM }), commentRanges, pendingCommentMark, ...(extra ? [extra] : [])] }), parent: document.body }));

/** A drawn table widget for TABLE; header and two body rows. */
function drawnTable(view: EditorView) {
	const el = document.createElement('div');
	el.className = 'cm-md-table';
	el.dataset.srcLength = String(TABLE.length);
	el.innerHTML = '<table><thead><tr><th>Item</th><th>Note</th></tr></thead><tbody><tr><td>Alpha</td><td>ab ab</td></tr><tr><td>Bravo</td><td>Eli</td></tr><tr><td>Charlie</td><td></td></tr></tbody></table>';
	view.posAtDOM = () => TABLE_FROM;
	return el;
}
const cell = (el: HTMLElement, row: number, col: number) => el.querySelector('table')!.rows[row].cells[col];

beforeEach(() => {
	// jsdom has no layout: a range reports no rects.
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	layout.getClientRects = () => [] as unknown as DOMRectList;
	layout.getBoundingClientRect = () => rect(0);
});
afterEach(() => {
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	delete layout.getClientRects;
	delete layout.getBoundingClientRect;
	document.body.replaceChildren();
	vi.useRealTimers();
});

describe('selectionIn a table', () => {
	it('maps a range inside one cell to its source offsets', () => {
		const view = open(), el = drawnTable(view), range = document.createRange();
		range.setStart(cell(el, 1, 0).firstChild!, 1);
		range.setEnd(cell(el, 1, 0).firstChild!, 4);
		const selection = selectionIn(view, el, range)!;
		const alpha = TABLE_FROM + TABLE.indexOf('Alpha');
		expect(selection).toMatchObject({ from: alpha + 1, to: alpha + 4, text: 'lph', inWidget: true });
		expect(label(selection)).toBe('“lph” · Item');
	});

	it('maps repeated text in a cell to the occurrence that was selected', () => {
		const view = open(), el = drawnTable(view), text = cell(el, 1, 1).firstChild!;
		const cellStart = TABLE_FROM + TABLE.indexOf('ab ab');
		for (const [start, offset] of [[0, 0], [3, 3]] as const) {
			const range = document.createRange();
			range.setStart(text, start);
			range.setEnd(text, start + 2);
			expect(selectionIn(view, el, range)).toMatchObject({ from: cellStart + offset, to: cellStart + offset + 2, text: 'ab' });
		}
	});

	it('maps a range over several cells of one row to those cells', () => {
		const view = open(), el = drawnTable(view), range = document.createRange();
		range.setStart(cell(el, 1, 0).firstChild!, 0);
		range.setEnd(cell(el, 1, 1).firstChild!, 5);
		const selection = selectionIn(view, el, range)!;
		const alpha = TABLE_FROM + TABLE.indexOf('Alpha');
		expect(selection).toMatchObject({ from: alpha, to: alpha + 'Alpha | ab ab'.length, text: 'Alpha | ab ab' });
		expect(label(selection)).toBe('2 cells, from “Alpha”');
	});

	it('snaps a range across rows to whole rows', () => {
		const view = open(), el = drawnTable(view), range = document.createRange();
		range.setStart(cell(el, 1, 0).firstChild!, 2);
		range.setEnd(cell(el, 2, 0).firstChild!, 3);
		const selection = selectionIn(view, el, range)!;
		const alpha = TABLE_FROM + TABLE.indexOf('Alpha'), eli = TABLE_FROM + TABLE.indexOf('Eli') + 3;
		expect(selection).toMatchObject({ from: alpha, to: eli, text: TABLE.slice(TABLE.indexOf('Alpha'), TABLE.indexOf('Eli') + 3) });
		expect(label(selection)).toBe('2 rows, from “Alpha”');
	});

	it('is null for a range holding no cell text', () => {
		const view = open(), el = drawnTable(view), range = document.createRange();
		range.setStart(cell(el, 1, 0).firstChild!, 2);
		range.setEnd(cell(el, 1, 0).firstChild!, 2);
		expect(selectionIn(view, el, range)).toBeNull();
	});

	it('maps a collapsed press in an empty cell to its structural source anchor', () => {
		const view = open(), el = drawnTable(view), range = document.createRange(), empty = cell(el, 3, 1);
		range.selectNodeContents(empty);
		const start = TABLE_FROM + TABLE.indexOf('||', TABLE.indexOf('Charlie'));
		expect(selectionIn(view, el, range)).toMatchObject({ from: start, to: start + 1, text: '|', inWidget: true });
		expect(label(selectionIn(view, el, range))).toBe('“Empty cell” · Charlie, Note');
	});
});

describe('selectionIn a diagram', () => {
	it('selects the whole fenced block, whatever was dragged across', () => {
		const view = open(), el = document.createElement('div'), range = document.createRange();
		el.className = 'cm-md-mermaid';
		el.textContent = 'A to B';
		document.body.append(el);
		view.posAtDOM = () => FENCE_FROM;
		range.selectNodeContents(el);
		const selection = selectionIn(view, el, range)!;
		expect(selection).toMatchObject({ from: FENCE_FROM, to: FENCE_TO, inWidget: true });
		expect(label(selection)).toBe('The whole diagram');
	});
});

describe('a selection raised inside a rendered table', () => {
	it('survives the editor updates that follow it when the editor held focus', () => {
		vi.useFakeTimers({ toFake: ['requestAnimationFrame'] });
		let view!: EditorView;
		const raised: Array<WidgetSelection | null> = [];
		// As the document viewer does: raising a widget selection draws its pending mark, an editor update that repaints the cell.
		const onSelect = (selection: WidgetSelection | null) => {
			raised.push(selection);
			view.plugin(pendingCommentMark)!.range = selection && { from: selection.from, to: selection.to };
			view.dispatch({ effects: setCommentRanges.of([]) });
		};
		view = parsed(new EditorView({ state: EditorState.create({ doc: DOC, extensions: [markdown({ extensions: GFM }), markdownLivePreview, commentRanges, pendingCommentMark, widgetComments({ onSelect, onFocus: vi.fn() })] }), parent: document.body }));
		view.focus();
		expect(view.hasFocus, 'setup: the editor holds focus, as after any click in the document').toBe(true);
		const table = view.contentDOM.querySelector<HTMLElement>('.cm-md-table')!;
		expect(table, 'setup: the table rendered').toBeTruthy();
		const text = table.querySelector('table')!.rows[1].cells[0].firstChild!;
		document.getSelection()!.setBaseAndExtent(text, 1, text, 4);
		document.dispatchEvent(new Event('selectionchange'));
		vi.advanceTimersByTime(50);
		document.dispatchEvent(new Event('selectionchange'));
		vi.advanceTimersByTime(50);
		expect(table.querySelector('.cm-comment-pending')?.textContent, 'the pending mark is drawn under the selection').toBe('lph');
		expect(document.getSelection()!.toString()).toBe('lph');
		expect(raised.at(-1)).toMatchObject({ text: 'lph', inWidget: true });
	});
});

describe('comments drawn on a diagram', () => {
	const hooks = () => ({ onSelect: vi.fn(), onFocus: vi.fn() });
	const range = (id: string): CommentRange => ({ id, from: FENCE_FROM, to: FENCE_TO });

	/** A mermaid widget in the editor's content, painted for the given comments and draft. */
	function paintedDiagram(ids: string[], draft: boolean) {
		vi.useFakeTimers({ toFake: ['requestAnimationFrame'] });
		const handlers = hooks(), view = open(widgetComments(handlers));
		const el = document.createElement('div');
		el.className = 'cm-md-mermaid';
		// Drawn outside CodeMirror's own content, which would drop a node it did not draw.
		const drawn = view.dom.appendChild(document.createElement('div'));
		drawn.append(el);
		Object.defineProperty(view, 'contentDOM', { value: drawn });
		view.posAtDOM = () => FENCE_FROM;
		if (draft) view.plugin(pendingCommentMark)!.range = { from: FENCE_FROM, to: FENCE_TO };
		view.dispatch({ effects: setCommentRanges.of(ids.map(range)) });
		vi.advanceTimersByTime(100);
		return { el, handlers };
	}

	it('carries every comment on the element and opens them all on a click', () => {
		const { el, handlers } = paintedDiagram(['a', 'b'], false);
		expect(el.dataset.commentIds).toBe('a,b');
		el.click();
		expect(handlers.onFocus).toHaveBeenCalledTimes(1);
		expect(handlers.onFocus).toHaveBeenCalledWith(['a', 'b'], el);
	});

	it('never routes a click to a pending draft over the same diagram', () => {
		const { el, handlers } = paintedDiagram(['a', 'b'], true);
		expect(el.classList.contains('cm-comment-block-pending')).toBe(true);
		el.click();
		expect(handlers.onFocus).toHaveBeenCalledWith(['a', 'b'], el);
		expect(handlers.onFocus.mock.calls.flat(2)).not.toContain('pending');
	});

	it('does not treat a diagram carrying only a draft as a comment to open', () => {
		const { el, handlers } = paintedDiagram([], true);
		expect(el.classList.contains('cm-comment-block-pending')).toBe(true);
		expect(el.dataset.commentIds).toBeUndefined();
		el.click();
		expect(handlers.onFocus).not.toHaveBeenCalled();
	});
});
