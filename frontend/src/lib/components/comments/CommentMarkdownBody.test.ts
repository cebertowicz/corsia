import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentMarkdownBody from './CommentMarkdownBody.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let resize: ResizeObserverCallback | undefined;

class ResizeObserverStub {
	constructor(callback: ResizeObserverCallback) {
		resize = callback;
	}
	observe() {}
	disconnect() {}
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	resize = undefined;
	vi.unstubAllGlobals();
});

describe('CommentMarkdownBody', () => {
	it('keeps the disclosure available after expanding and collapsing a long body', () => {
		vi.stubGlobal('ResizeObserver', ResizeObserverStub);
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
			callback(0);
			return 0;
		});
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(CommentMarkdownBody, { target: host, props: { text: 'A long comment body' } });
		flushSync();

		const reveal = host.querySelector<HTMLElement>('.ct-markdown-reveal')!;
		const markdown = host.querySelector<HTMLElement>('.ct-markdown')!;
		Object.defineProperties(reveal, {
			clientHeight: { configurable: true, get: () => 64 },
			scrollHeight: { configurable: true, get: () => 320 }
		});
		Object.defineProperty(markdown, 'scrollHeight', { configurable: true, get: () => 320 });
		resize?.([], {} as ResizeObserver);
		flushSync();

		const more = () => host.querySelector<HTMLButtonElement>('.ct-more')!;
		expect(more().textContent).toBe('Show more');
		expect(reveal.classList.contains('clamped')).toBe(true);
		more().click();
		flushSync();
		expect(more().textContent).toBe('Show less');
		expect(reveal.classList.contains('open')).toBe(true);
		expect(reveal.classList.contains('clamped')).toBe(false);
		more().click();
		flushSync();
		expect(more().textContent).toBe('Show more');
		expect(reveal.classList.contains('open')).toBe(false);
		expect(reveal.classList.contains('clamped')).toBe(true);
	});

	it('toggling the disclosure does not reach the card that focuses its thread', () => {
		vi.stubGlobal('ResizeObserver', ResizeObserverStub);
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
			callback(0);
			return 0;
		});
		host = document.createElement('div');
		document.body.appendChild(host);
		const card = vi.fn();
		host.addEventListener('click', card);
		const inner = document.createElement('div');
		host.appendChild(inner);
		component = mount(CommentMarkdownBody, { target: inner, props: { text: 'A long comment body' } });
		flushSync();
		const reveal = host.querySelector<HTMLElement>('.ct-markdown-reveal')!;
		Object.defineProperties(reveal, {
			clientHeight: { configurable: true, get: () => 64 },
			scrollHeight: { configurable: true, get: () => 320 }
		});
		Object.defineProperty(host.querySelector('.ct-markdown'), 'scrollHeight', { configurable: true, get: () => 320 });
		resize?.([], {} as ResizeObserver);
		flushSync();
		host.querySelector<HTMLButtonElement>('.ct-more')!.click();
		flushSync();
		expect(card).not.toHaveBeenCalled();
		host.removeEventListener('click', card);
	});
});
