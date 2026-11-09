import { afterEach, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import DocMarginStack from './DocMarginStack.svelte';
import type { DocComment } from '$lib/stores/docComments';

const card = createRawSnippet<[{ id: string }]>((comment) => ({ render: () => `<p>${comment().id}</p>` }));
const thread = (id: string) => ({ id, anchor: 'quote', author: 'you', text: id, at: 'now', resolved: false, replies: [] }) as DocComment;

let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

it('shows every card once its column is on the canvas, even when a card was hidden while the column followed the scroll beside it', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	const bounds = document.createElement('div');
	bounds.innerHTML = '<div class="cm-scroller"><div class="cm-content"></div></div>';
	const scroller = bounds.querySelector<HTMLElement>('.cm-scroller')!;
	const clip = document.createElement('div');
	clip.className = 'drawer-comments';
	document.body.append(bounds, clip);
	const props = $state({ bounds, pinned: [thread('a'), thread('b')], selectedId: null as string | null, behind: false, card });
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
