import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import type { CommentRange } from '$lib/cm/comment-marks';
import MarkdownEditor from './MarkdownEditor.svelte';
import * as geometry from '$lib/cm/comment-anchor-geometry';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { EditorView } from '@codemirror/view';

const DOC = 'intro\n\n| Item | Owner |\n|---|---|\n| Alpha | Dana |\n| Bravo | Eli |\n\nafter\n';
const DANA = DOC.indexOf('Dana');

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;

function mountEditor(highlights: CommentRange[], onAnchorFocus = vi.fn(), props: { onCaretAway?: () => void } = {}) {
	host = document.body.appendChild(document.createElement('div'));
	component = mount(MarkdownEditor, { target: host, props: { value: DOC, highlights, onAnchorFocus, ...props } });
	flushSync();
	return onAnchorFocus;
}
const comment = (id: string, onMarkerFocus?: CommentRange['onMarkerFocus']): CommentRange => ({ id, from: DANA, to: DANA + 4, onMarkerFocus });
/** The innermost mark drawn on the text, once the editor has painted it: with several comments they nest. */
const painted = () => vi.waitFor(() => {
	const marks = host.querySelectorAll<HTMLElement>('.cm-md-table [data-comment-ids]');
	if (!marks.length) throw new Error('not painted');
	return marks[marks.length - 1];
});

beforeEach(() => {
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	layout.getClientRects = () => [] as unknown as DOMRectList;
	layout.getBoundingClientRect = () => new DOMRect();
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	focusedDocComment.set(null);
	vi.restoreAllMocks();
	host?.remove();
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	delete layout.getClientRects;
	delete layout.getBoundingClientRect;
});

describe('a click on comments drawn inside a table', () => {
	it('opens one comment as a click on its text does', async () => {
		const onMarkerFocus = vi.fn();
		const onAnchorFocus = mountEditor([comment('a', onMarkerFocus)]);
		(await painted()).click();
		expect(onAnchorFocus).toHaveBeenCalledWith('a', expect.any(Function), undefined, expect.any(MouseEvent));
		expect(onMarkerFocus).not.toHaveBeenCalled();
	});

	it('opens several comments on the same text as their margin marker does', async () => {
		const onMarkerFocus = vi.fn();
		const onAnchorFocus = mountEditor([comment('a', onMarkerFocus), comment('b', onMarkerFocus)]);
		(await painted()).click();
		// The click goes on with them, so the cards open at it, as a click on one comment's text does.
		expect(onMarkerFocus).toHaveBeenCalledWith(['a', 'b'], expect.any(HTMLElement), expect.any(Function), expect.any(MouseEvent), false);
		expect(onAnchorFocus).not.toHaveBeenCalled();
	});

	it('anchors a marker to the focused thread or the first surviving candidate', async () => {
		const onMarkerFocus = vi.fn();
		const rows = vi.spyOn(geometry, 'rowsOf').mockReturnValue(new DOMRect());
		mountEditor([comment('a', onMarkerFocus), { ...comment('b', onMarkerFocus), to: DANA + 3 }]);
		(await painted()).click();
		const anchor = onMarkerFocus.mock.calls[0][2] as () => DOMRect | null;
		focusedDocComment.set('a');
		anchor();
		expect(rows).toHaveBeenLastCalledWith(expect.anything(), DANA, DANA + 4, null);
		focusedDocComment.set('gone');
		anchor();
		expect(rows).toHaveBeenLastCalledWith(expect.anything(), DANA, DANA + 3, null);
	});

	it('keeps the comment open and the caret in place when the table redraws before the browser reports where the press landed', async () => {
		const onCaretAway = vi.fn();
		const onAnchorFocus = mountEditor([comment('a')], vi.fn(), { onCaretAway });
		const mark = await painted();
		const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
		// The reader's caret rests in the text above the table.
		view.focus();
		view.dispatch({ selection: { anchor: 2 } });
		flushSync();
		onCaretAway.mockClear();
		const landed = mark.firstChild!;
		const caretPosition = vi.fn(() => ({ offsetNode: landed, offset: 1 }) as unknown as CaretPosition);
		Object.defineProperty(document, 'caretPositionFromPoint', { configurable: true, value: caretPosition });
		try {
			// A click on the comment: the table draws the press's caret in the cell, and the click opens the comment.
			mark.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, detail: 1 }));
			window.dispatchEvent(new MouseEvent('mouseup'));
			mark.click();
			expect(caretPosition).toHaveBeenCalled();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			// The table redraws its marks for the opened comment before the browser's selectionchange arrives.
			mark.classList.add('cm-comment-focused');
			await Promise.resolve();
			flushSync();
			expect(view.state.selection.main.head, 'the editor caret did not jump to the table').toBe(2);
			expect(onCaretAway, 'the comment the click opened was not let go').not.toHaveBeenCalled();
		} finally {
			delete (document as Partial<Document>).caretPositionFromPoint;
		}
	});
});

describe('a suggestion drawn inside a table cell', () => {
	const suggestion = (inline = true): CommentRange => ({
		id: 's1', from: DANA, to: DANA + 4, suggestion: { quote: 'Dana', insert: 'Dan', color: 'oklch(0.6 0.15 250)', inline, busy: false },
	});
	const struck = () => vi.waitFor(() => {
		const el = host.querySelector<HTMLElement>('.cm-md-table .cm-suggestion-del');
		if (!el) throw new Error('not painted');
		return el;
	});

	it('strikes the quote and shows the insert after it, as in body text', async () => {
		mountEditor([suggestion()]);
		const del = await struck();
		expect(del.textContent).toBe('Dana');
		const insert = host.querySelector<HTMLElement>('.cm-md-table .cm-suggestion-insert');
		expect(insert?.dataset.insert).toBe('Dan');
		expect(insert?.dataset.suggestionId).toBe('s1');
		expect(insert?.getAttribute('aria-hidden')).toBe('true');
	});

	it('draws nothing for a suggestion that is card-only because an earlier one overlaps it', async () => {
		mountEditor([suggestion(false)]);
		await new Promise((resolve) => setTimeout(resolve, 50));
		expect(host.querySelector('.cm-md-table .cm-suggestion-del')).toBeNull();
	});

	it('opens its card as a suggestion when the struck text is clicked', async () => {
		const onAnchorFocus = mountEditor([suggestion()]);
		(await struck()).click();
		expect(onAnchorFocus).toHaveBeenCalledWith('s1', expect.any(Function), true, expect.any(MouseEvent));
	});
});
