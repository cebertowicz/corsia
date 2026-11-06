import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import type { CommentRange } from '$lib/cm/comment-marks';
import MarkdownEditor from './MarkdownEditor.svelte';

const DOC = 'intro\n\n| Item | Owner |\n|---|---|\n| Alpha | Dana |\n| Bravo | Eli |\n\nafter\n';
const DANA = DOC.indexOf('Dana');

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;

function mountEditor(highlights: CommentRange[], onAnchorFocus = vi.fn()) {
	host = document.body.appendChild(document.createElement('div'));
	component = mount(MarkdownEditor, { target: host, props: { value: DOC, highlights, onAnchorFocus } });
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
		expect(onAnchorFocus).toHaveBeenCalledWith('a', expect.any(Function), undefined);
		expect(onMarkerFocus).not.toHaveBeenCalled();
	});

	it('opens several comments on the same text as their margin marker does', async () => {
		const onMarkerFocus = vi.fn();
		const onAnchorFocus = mountEditor([comment('a', onMarkerFocus), comment('b', onMarkerFocus)]);
		(await painted()).click();
		expect(onMarkerFocus).toHaveBeenCalledWith(['a', 'b'], expect.any(HTMLElement), expect.any(Function));
		expect(onAnchorFocus).not.toHaveBeenCalled();
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
		expect(onAnchorFocus).toHaveBeenCalledWith('s1', expect.any(Function), true);
	});
});
