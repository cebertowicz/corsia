import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import DocMarginStack from './DocMarginStack.svelte';
import type { DocComment } from '$lib/stores/docComments';
import type { MarginAnchorSource, MarginComposer } from '$lib/docMarginSource';

const card = createRawSnippet<[{ id: string }]>((comment) => ({ render: () => `<p>${comment().id}</p>` }));
const thread = (id: string) => ({ id, anchor: 'quote', author: 'you', text: id, at: 'now', resolved: false, replies: [] }) as DocComment;

let component: ReturnType<typeof mount> | null = null;

// jsdom draws nothing, so every element would read as having no box; here each one is drawn unless a test says otherwise.
beforeEach(() => { vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList); });

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('DocMarginStack', () => {
	it('places its cards without setting its observers up a second time', () => {
		let observers = 0;
		class CountingMutationObserver extends MutationObserver {
			constructor(callback: MutationCallback) { super(callback); observers++; }
		}
		vi.stubGlobal('MutationObserver', CountingMutationObserver);
		vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
		const bounds = document.createElement('div');
		bounds.innerHTML = '<div class="cm-scroller"><div class="cm-content"></div></div>';
		const column = document.createElement('div');
		column.className = 'drawer-comments';
		bounds.querySelector('.cm-scroller')!.append(column);
		document.body.append(bounds);

		component = mount(DocMarginStack, { target: column, props: { bounds, pinned: [thread('a'), thread('b')], selectedId: null, card } });
		flushSync();

		// The first placement is a change of `tops`; it must not tear the setup down and build it again.
		expect(column.querySelectorAll('.margin-card.placed')).toHaveLength(2);
		expect(observers).toBe(1);
	});

	it('draws a comment being written as a card level with its text, picked, and ready to type', async () => {
		vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
		const scroller = document.createElement('div');
		const column = document.createElement('div');
		scroller.append(column);
		document.body.append(scroller);
		const mark = document.createElement('span');
		scroller.append(mark);
		mark.getBoundingClientRect = () => ({ top: 40 }) as DOMRect;
		const source: MarginAnchorSource = { scroller: () => scroller, column: () => column, anchor: (id) => id === 'a' ? mark : null, moved: () => [], resized: () => [] };
		const body = createRawSnippet(() => ({ render: () => '<div><textarea aria-label="Comment"></textarea></div>' }));
		const composer: MarginComposer = { id: 'composer', anchor: () => ({ top: 160 }) as DOMRect, body };
		component = mount(DocMarginStack, { target: column, props: { source, pinned: [thread('a')], selectedId: null, card, composer } });
		flushSync();
		await Promise.resolve();

		const writing = column.querySelector<HTMLElement>('[data-margin-card="composer"]')!;
		expect(writing.classList.contains('margin-composer')).toBe(true);
		expect(writing.style.top).toBe('160px');
		expect(column.querySelector<HTMLElement>('[data-margin-card="a"]')!.style.top).toBe('40px');
		expect(document.activeElement).toBe(writing.querySelector('textarea'));
	});

	it('draws its head as the column\'s first card, always at the top: no card goes under it, and a card picked on text above its foot sits just under it', () => {
		vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
		const scroller = document.createElement('div');
		const column = document.createElement('div');
		scroller.append(column);
		document.body.append(scroller);
		const mark = document.createElement('span');
		scroller.append(mark);
		mark.getBoundingClientRect = () => ({ top: 10 }) as DOMRect;
		vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.hasAttribute('data-margin-head') ? 40 : 0; });
		const source: MarginAnchorSource = { scroller: () => scroller, column: () => column, anchor: (id) => id === 'a' ? mark : null, moved: () => [], resized: () => [] };
		const head = createRawSnippet(() => ({ render: () => '<button>Accept all from Sam</button>' }));
		const place = (selectedId: string | null) => {
			if (component) unmount(component);
			component = mount(DocMarginStack, { target: column, props: { source, pinned: [thread('a')], selectedId, card, head } });
			flushSync();
			const top = column.querySelector<HTMLElement>('.margin-stack > [data-margin-head]')!;
			expect(top.classList.contains('placed')).toBe(true);
			expect(top.textContent).toBe('Accept all from Sam');
			return { head: top.style.top, card: column.querySelector<HTMLElement>('[data-margin-card="a"]')!.style.top };
		};

		expect(place(null)).toEqual({ head: '0px', card: '48px' });
		expect(place('a')).toEqual({ head: '0px', card: '48px' });
		vi.restoreAllMocks();
	});

	it('places and shows no card while its column is not drawn yet, then places each level with its text once the column is', () => {
		const observed: Array<() => void> = [];
		vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { observed.push(callback); } observe() {} unobserve() {} disconnect() {} });
		const scroller = document.createElement('div');
		const column = document.createElement('div');
		scroller.append(column);
		document.body.append(scroller);
		const mark = document.createElement('span');
		scroller.append(mark);
		// The text is scrolled: its line is 300px down the window, and the column, once drawn, starts 200px above the window top.
		mark.getBoundingClientRect = () => ({ top: 300 }) as DOMRect;
		let drawn = false;
		column.getClientRects = () => (drawn ? [{}] : []) as unknown as DOMRectList;
		column.getBoundingClientRect = () => ({ top: drawn ? -200 : 0 }) as DOMRect;
		const source: MarginAnchorSource = { scroller: () => scroller, column: () => column, anchor: (id) => id === 'a' ? mark : null, moved: () => [], resized: () => [] };
		const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((step) => { step(0); return 1; });
		component = mount(DocMarginStack, { target: column, props: { source, pinned: [thread('a')], selectedId: null, card } });
		flushSync();

		const placed = () => column.querySelector<HTMLElement>('[data-margin-card="a"]')!;
		expect(placed().classList.contains('placed')).toBe(false);

		drawn = true;
		observed.forEach((callback) => callback());
		flushSync();
		expect(placed().classList.contains('placed')).toBe(true);
		expect(placed().style.top).toBe('500px');
		raf.mockRestore();
	});

	it('brings a picked card into view from the place it is given, not the place its box held before the pick', () => {
		vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
		const scroller = document.createElement('div');
		const column = document.createElement('div');
		scroller.append(column);
		document.body.append(scroller);
		const scrollBy = vi.fn();
		scroller.scrollBy = scrollBy as unknown as typeof scroller.scrollBy;
		const marks: Record<string, HTMLElement> = {};
		for (const [id, top] of [['a', 100], ['b', 400]] as const) {
			marks[id] = document.createElement('span');
			marks[id].getBoundingClientRect = () => ({ top }) as DOMRect;
			scroller.append(marks[id]);
		}
		vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.hasAttribute('data-margin-card') ? 120 : 0; });
		// The card's box is where it sat before this pick, low in the stack and below the view, until the new place is drawn.
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
			return (this.hasAttribute('data-margin-card') ? { top: 900, bottom: 1020 } : { top: 0, bottom: 0 }) as DOMRect;
		});
		const view = { top: 0, bottom: 600 };
		const source: MarginAnchorSource = { scroller: () => scroller, column: () => column, anchor: (id) => marks[id] ?? null, moved: () => [], resized: () => [], view: () => view };
		const pick = (selectedId: string) => {
			if (component) unmount(component);
			component = mount(DocMarginStack, { target: column, props: { source, pinned: [thread('a'), thread('b')], selectedId, card } });
			flushSync();
		};

		// Placed at its line (400 to 520), the card is in view: the text does not move.
		pick('b');
		expect(column.querySelector<HTMLElement>('[data-margin-card="b"]')!.style.top).toBe('400px');
		expect(scrollBy).not.toHaveBeenCalled();

		// Placed where its foot passes the view's, the text moves by exactly that much.
		marks.b.getBoundingClientRect = () => ({ top: 500 }) as DOMRect;
		pick('b');
		expect(scrollBy).toHaveBeenCalledTimes(1);
		expect(scrollBy.mock.calls[0][0].top).toBe(500 + 120 + 8 - 600);
		vi.restoreAllMocks();
	});
});
