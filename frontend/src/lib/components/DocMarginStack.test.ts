import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import DocMarginStack from './DocMarginStack.svelte';
import type { DocComment } from '$lib/stores/docComments';

const card = createRawSnippet<[DocComment]>((comment) => ({ render: () => `<p>${comment().id}</p>` }));
const thread = (id: string) => ({ id, anchor: 'quote', author: 'you', text: id, at: 'now', resolved: false, replies: [] }) as DocComment;

let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
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
		const clip = document.createElement('div');
		clip.className = 'drawer-comments';
		document.body.append(bounds, clip);

		component = mount(DocMarginStack, { target: clip, props: { bounds, pinned: [thread('a'), thread('b')], selectedId: null, behind: false, card } });
		flushSync();

		// The first placement is a change of `tops`; it must not tear the setup down and build it again.
		expect(clip.querySelectorAll('.margin-card.placed')).toHaveLength(2);
		expect(observers).toBe(1);
	});

	it.each([
		['inside the scroller (the document canvas): the cards ride the native scroll and nothing follows it', true, ''],
		['beside the scroller (the chat column): the stack follows the scroll', false, 'translateY(-200px)'],
	])('with its clip %s', (_where, inside, transform) => {
		vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
		const bounds = document.createElement('div');
		bounds.innerHTML = '<div class="cm-scroller"><div class="cm-content"></div></div>';
		const scroller = bounds.querySelector<HTMLElement>('.cm-scroller')!;
		const clip = document.createElement('div');
		clip.className = 'drawer-comments';
		document.body.append(bounds);
		(inside ? scroller : document.body).append(clip);
		component = mount(DocMarginStack, { target: clip, props: { bounds, pinned: [thread('a')], selectedId: null, behind: false, card } });
		flushSync();
		Object.defineProperty(scroller, 'scrollTop', { configurable: true, value: 200 });
		scroller.dispatchEvent(new Event('scroll'));
		expect(clip.querySelector<HTMLElement>('.margin-stack')!.style.transform).toBe(transform);
	});
});
