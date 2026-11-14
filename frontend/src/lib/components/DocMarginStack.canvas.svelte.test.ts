import { afterEach, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import DocMarginStack from './DocMarginStack.svelte';
import type { DocComment } from '$lib/stores/docComments';

const snippet = createRawSnippet<[{ id: string }]>((comment) => ({ render: () => `<p>${comment().id}</p>` }));
const thread = (id: string) => ({ id, anchor: 'quote', author: 'you', text: id, at: 'now', resolved: false, replies: [] }) as DocComment;

let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

it('scrolls a picked card on the canvas that is taller than the window until its Send button is above the window bottom', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	// jsdom draws nothing; the column here is drawn on the canvas.
	vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
	// A wide document (1440 x 900): the editor's scroller sits under the 100px header, and the picked card holds a 1085px reply.
	const bounds = document.createElement('div');
	bounds.innerHTML = '<div class="cm-scroller"><div class="cm-content"></div></div>';
	const scroller = bounds.querySelector<HTMLElement>('.cm-scroller')!;
	const column = document.createElement('div');
	column.className = 'drawer-comments';
	scroller.append(column);
	document.body.append(bounds);
	const windowHeight = 900, card = { top: 250, bottom: 250 + 1085 + 95 };
	let scrolled = 0;
	scroller.scrollBy = ((options: ScrollToOptions) => { scrolled += options.top ?? 0; }) as typeof scroller.scrollBy;
	// The card is placed at the column's top (its line is unknown here), and the column sits 250px down the window; the card is
	// brought into view from that place and its height, as it will be drawn.
	vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.dataset.marginCard === 'a' ? card.bottom - card.top : 0; });
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		if (this === scroller) return { top: 100, bottom: windowHeight } as DOMRect;
		// The column's own box on the canvas is no view: it sits at the top of the text and has no height.
		if (this === column) return { top: card.top, bottom: card.top } as DOMRect;
		return { top: 0, bottom: 0 } as DOMRect;
	});
	component = mount(DocMarginStack, { target: column, props: { bounds, pinned: [thread('a')], selectedId: 'a', card: snippet } });
	flushSync();

	// Send ends inside the window, under the header: not left below the fold, nor scrolled past.
	expect(card.bottom - scrolled).toBeLessThanOrEqual(windowHeight);
	expect(card.bottom - scrolled).toBeGreaterThan(100);
});
