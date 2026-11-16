import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import DocMarginStack from './DocMarginStack.svelte';
import type { DocComment } from '$lib/stores/docComments';
import type { MarginAnchorSource } from '$lib/docMarginSource';

const card = createRawSnippet<[{ id: string }]>((comment) => ({ render: () => `<div><p>${comment().id}</p><textarea aria-label="Reply"></textarea></div>` }));
const thread = (id: string) => ({ id, anchor: 'quote', author: 'you', text: id, at: 'now', resolved: false, replies: [] }) as DocComment;

let component: ReturnType<typeof mount> | null = null;
let now = 0;
let height = 120;
const observed: Array<() => void> = [];

beforeEach(() => {
	now = 0; height = 120; observed.length = 0;
	vi.spyOn(performance, 'now').mockImplementation(() => now);
	vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
	// Each frame runs at once; handing back 0 leaves the stack free to ask for the next one, as a real frame that ran does.
	vi.spyOn(window, 'requestAnimationFrame').mockImplementation((step) => { step(0); return 0; });
	vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { observed.push(callback); } observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.hasAttribute('data-margin-card') ? height : 0; });
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

/** A transcript with card `a` picked level with its line at 400, in a 600px view; returns what scrolled it. */
function pickedCard() {
	const scroller = document.createElement('div');
	const column = document.createElement('div');
	scroller.append(column);
	document.body.append(scroller);
	const scrollBy = vi.fn();
	scroller.scrollBy = scrollBy as unknown as typeof scroller.scrollBy;
	const mark = document.createElement('span');
	mark.getBoundingClientRect = () => ({ top: 400 }) as DOMRect;
	scroller.append(mark);
	column.getBoundingClientRect = () => ({ top: 0, bottom: 0 }) as DOMRect;
	const source: MarginAnchorSource = { scroller: () => scroller, column: () => column, anchor: (id) => (id === 'a' ? mark : null), moved: () => [], resized: () => [], view: () => ({ top: 0, bottom: 600 }) };
	const props = $state({ source, pinned: [thread('a')], selectedId: 'a' as string | null, card });
	component = mount(DocMarginStack, { target: column, props });
	flushSync();
	// The pick's own moment is over: from here on only a growth of the card can scroll the text.
	now = 2_000;
	const field = column.querySelector<HTMLTextAreaElement>('[data-margin-card="a"] textarea')!;
	const resize = () => { observed.forEach((callback) => callback()); flushSync(); };
	return { scrollBy, props, field, resize };
}

it('keeps the text still when a reply arrives in a picked card the reader is reading, and when a refresh hands it new comment objects', () => {
	const { scrollBy, props, field, resize } = pickedCard();
	expect(scrollBy).not.toHaveBeenCalled();
	// A click on the card's text leaves focus resting in it: that is reading.
	field.focus();
	// A refresh brings the same comment as a new object, and the column re-runs.
	props.pinned = [thread('a')];
	flushSync();
	resize();
	expect(scrollBy).not.toHaveBeenCalled();
	// A reply lands and the card grows past the view's end.
	height = 400;
	resize();
	expect(scrollBy).not.toHaveBeenCalled();
});

it('brings the card\'s end into view when it grows from the reader\'s own typing in it', () => {
	const { scrollBy, field, resize } = pickedCard();
	field.focus();
	field.dispatchEvent(new Event('input', { bubbles: true }));
	height = 400;
	resize();
	expect(scrollBy).toHaveBeenCalledTimes(1);
	expect(scrollBy.mock.calls[0][0].top).toBe(400 + 400 + 8 - 600);
	// Input from a while ago does not count for a later growth.
	now += 5_000;
	height = 500;
	resize();
	expect(scrollBy).toHaveBeenCalledTimes(1);
});
