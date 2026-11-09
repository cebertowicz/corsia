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

it('shows every card once its column is on the canvas, even when a card was hidden while the column followed the scroll beside it', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	const bounds = document.createElement('div');
	bounds.innerHTML = '<div class="cm-scroller"><div class="cm-content"></div></div>';
	const scroller = bounds.querySelector<HTMLElement>('.cm-scroller')!;
	const clip = document.createElement('div');
	clip.className = 'drawer-comments';
	document.body.append(bounds, clip);
	const props = $state({ bounds, pinned: [thread('a'), thread('b')], selectedId: null as string | null, behind: false, card: snippet });
	component = mount(DocMarginStack, { target: clip, props });
	flushSync();

	// Beside the scroller, a card scrolled above the clip is hidden.
	Object.defineProperty(scroller, 'scrollTop', { configurable: true, value: 200 });
	scroller.dispatchEvent(new Event('scroll'));
	expect(clip.querySelectorAll('.margin-card.under')).toHaveLength(2);

	// The column moves onto the canvas (as `marginOnCanvas` does after mount), and a pick sets the stack up again.
	scroller.append(clip);
	props.selectedId = 'b';
	flushSync();
	scroller.dispatchEvent(new Event('scroll'));

	expect(clip.querySelectorAll('.margin-card.under')).toHaveLength(0);
	expect([...clip.querySelectorAll<HTMLElement>('.margin-stack, .margin-card')].map((el) => el.style.transform)).toEqual(['', '', '']);
});

it('scrolls a picked card on the canvas that is taller than the window until its Send button is above the window bottom', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	// A wide document (1440 x 900): the editor's scroller sits under the 100px header, and the picked card holds a 1085px reply.
	const bounds = document.createElement('div');
	bounds.innerHTML = '<div class="cm-scroller"><div class="cm-content"></div></div>';
	const scroller = bounds.querySelector<HTMLElement>('.cm-scroller')!;
	const clip = document.createElement('div');
	clip.className = 'drawer-comments';
	scroller.append(clip);
	document.body.append(bounds);
	const windowHeight = 900, card = { top: 250, bottom: 250 + 1085 + 95 };
	let scrolled = 0;
	scroller.scrollBy = ((options: ScrollToOptions) => { scrolled += options.top ?? 0; }) as typeof scroller.scrollBy;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		if (this === scroller) return { top: 100, bottom: windowHeight } as DOMRect;
		if (this.dataset.marginCard === 'a') return card as DOMRect;
		// The column's own box on the canvas is no view: it sits at the top of the text and has no height.
		return { top: 0, bottom: 0 } as DOMRect;
	});
	component = mount(DocMarginStack, { target: clip, props: { bounds, pinned: [thread('a')], selectedId: 'a', behind: false, card: snippet } });
	flushSync();

	// Send ends inside the window, under the header: not left below the fold, nor scrolled past.
	expect(card.bottom - scrolled).toBeLessThanOrEqual(windowHeight);
	expect(card.bottom - scrolled).toBeGreaterThan(100);
});
