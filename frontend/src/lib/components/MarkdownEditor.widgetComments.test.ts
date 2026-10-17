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
		expect(onAnchorFocus).toHaveBeenCalledWith('a', expect.any(Function));
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
