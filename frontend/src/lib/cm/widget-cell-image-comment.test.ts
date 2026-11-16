import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { forceParsing } from '@codemirror/language';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import { commentRanges } from './comment-marks';
import { markdownLivePreview } from './markdown-live';
import { pendingCommentMark } from './pending-comment-mark';
import { imageContext, type ImageContext } from './markdown-image';
import { selectionIn } from './widget-selection';
import { widgetComments, type WidgetSelection } from './widget-comments';
import { paintTable } from './widget-paint';

// A status column whose cells hold only a badge image, as a briefing's state column does.
const CONTEXT: ImageContext = { docPath: '/ws/notes/brief.md', projectId: 'p1', token: 't' };
const BADGE = '![Design](brief.assets/design.svg)';
const OTHER = '![Done](brief.assets/done.svg)';
const TABLE = `| Item | Status |\n| --- | --- |\n| Alpha | ${BADGE} |\n| Bravo | Done ${OTHER} |`;
const DOC = `intro\n\n${TABLE}\n\nafter\n`;
const at = (text: string) => DOC.indexOf(text);

const label = (selection: WidgetSelection | null) => typeof selection?.label === 'function' ? selection.label() : selection?.label;
const views: EditorView[] = [];

function mount(extra: Extension[] = [], context: ImageContext | null = CONTEXT): EditorView {
	const view = new EditorView({
		state: EditorState.create({ doc: DOC, extensions: [markdown({ extensions: GFM }), markdownLivePreview, commentRanges, pendingCommentMark, imageContext.of(() => context), ...extra] }),
		parent: document.body,
	});
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	views.push(view);
	return view;
}
const table = (view: EditorView) => view.contentDOM.querySelector<HTMLElement>('.cm-md-table')!;
const cell = (view: EditorView, row: number, col: number) => table(view).querySelector('table')!.rows[row].cells[col];

beforeEach(() => {
	// jsdom has no layout: a range reports no rects.
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	layout.getClientRects = () => [] as unknown as DOMRectList;
	layout.getBoundingClientRect = () => new DOMRect(0, 0, 100, 40);
});
afterEach(() => {
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	delete layout.getClientRects;
	delete layout.getBoundingClientRect;
	while (views.length) views.pop()!.destroy();
	document.body.replaceChildren();
	vi.useRealTimers();
});

describe('a selection over a table cell holding only an image', () => {
	it('is drawn as a cell image with no text, so the cell has nothing to select but its image', () => {
		const badge = cell(mount(), 1, 1);
		expect(badge.querySelector('.cm-md-cell-image img')?.getAttribute('alt')).toBe('Design');
		expect(badge.textContent?.trim()).toBe('');
	});

	it('comments on the image’s markdown, labelled by its alt text, row and column', () => {
		const view = mount(), range = document.createRange();
		range.selectNodeContents(cell(view, 1, 1));
		const selection = selectionIn(view, table(view), range);
		expect(selection).toMatchObject({ from: at(BADGE), to: at(BADGE) + BADGE.length, text: BADGE, inWidget: true });
		expect(label(selection)).toBe('“Design” · Alpha, Status');
	});

	it('comments on the whole cell when the selection covers only the image of a cell that also has text', () => {
		// Such a cell draws a mark on its text only, so a comment on just its image would be drawn on words it is not about.
		const view = mount(), range = document.createRange(), mixed = cell(view, 2, 1);
		range.selectNode(mixed.querySelector('.cm-md-cell-image')!);
		expect(selectionIn(view, table(view), range)).toMatchObject({ from: at(`Done ${OTHER}`), to: at(OTHER) + OTHER.length, text: `Done ${OTHER}` });
	});

	it('takes the whole cell when its image cannot be read back to its markdown', () => {
		// With no document to resolve against, the image is drawn as its alt text and nothing maps it back; the cell still takes the comment.
		const view = mount([], null), range = document.createRange(), badge = cell(view, 1, 1);
		badge.replaceChildren(Object.assign(document.createElement('span'), { className: 'cm-md-cell-image' }));
		range.selectNodeContents(badge);
		expect(selectionIn(view, table(view), range)).toMatchObject({ from: at(BADGE), to: at(BADGE) + BADGE.length, text: BADGE });
	});

	it('includes the image cell in a selection that runs from a text cell across it', () => {
		const view = mount(), range = document.createRange();
		range.setStart(cell(view, 1, 0).firstChild!, 0);
		range.setEnd(cell(view, 1, 1), cell(view, 1, 1).childNodes.length);
		const selection = selectionIn(view, table(view), range);
		expect(selection).toMatchObject({ from: at('Alpha'), to: at(BADGE) + BADGE.length });
		expect(label(selection)).toBe('2 cells, from “Alpha”');
	});

	it('is still null for a collapsed caret in a cell with text', () => {
		const view = mount(), caret = document.createRange(), text = cell(view, 1, 0).firstChild!;
		caret.setStart(text, 2);
		caret.setEnd(text, 2);
		expect(selectionIn(view, table(view), caret)).toBeNull();
	});
});

describe('the comment a selection over a cell’s image makes', () => {
	const paintFrom = (view: EditorView, selection: WidgetSelection | null) =>
		paintTable(table(view), at(TABLE), TABLE, [{ id: 'a', kind: 'placed', from: selection!.from, to: selection!.to }]);

	it('is drawn on the whole image-only cell it was made in', () => {
		const view = mount(), range = document.createRange(), badge = cell(view, 1, 1);
		range.selectNodeContents(badge);
		paintFrom(view, selectionIn(view, table(view), range));
		expect(badge.classList.contains('cm-comment-anchor')).toBe(true);
		expect(badge.dataset.commentIds).toBe('a');
		expect(badge.getAttribute('aria-label')).toMatch(/^Commented cell/);
		expect(table(view).querySelector('.cm-comment-wrap')).toBeNull();
	});

	it('is drawn on the text of a cell that also has text, as a comment on that whole cell', () => {
		const view = mount(), range = document.createRange(), mixed = cell(view, 2, 1);
		range.selectNode(mixed.querySelector('.cm-md-cell-image')!);
		const selection = selectionIn(view, table(view), range);
		paintFrom(view, selection);
		const drawn = [...mixed.querySelectorAll('.cm-comment-wrap[data-comment-ids="a"]')].map((wrap) => wrap.textContent?.trim());
		expect(drawn).toEqual(['Done']);
		// The words it is drawn on are words it is about, not just its image's alt text.
		expect(selection?.text.replace(OTHER, '')).toContain(drawn[0]);
		expect(mixed.textContent?.trim()).toBe('Done');
	});
});

describe('a press beside the image in an image-only cell', () => {
	it('raises a comment on the image’s markdown', () => {
		vi.useFakeTimers({ toFake: ['requestAnimationFrame'] });
		const onSelect = vi.fn();
		const view = mount([widgetComments({ onSelect, onFocus: vi.fn() })]);
		const badge = cell(view, 1, 1);
		badge.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, detail: 1 }));
		window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, button: 0 }));
		vi.advanceTimersByTime(50);
		expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ from: at(BADGE), to: at(BADGE) + BADGE.length, text: BADGE, inWidget: true }));
	});
});
