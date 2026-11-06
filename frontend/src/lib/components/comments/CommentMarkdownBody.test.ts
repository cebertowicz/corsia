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

	it('renders a tag of the person as a chip with their own account name', () => {
		vi.stubGlobal('ResizeObserver', ResizeObserverStub);
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(CommentMarkdownBody, {
			target: host,
			props: { text: 'Needs your call', taggedPerson: 'Sam' }
		});
		flushSync();

		const chips = [...host.querySelectorAll<HTMLElement>('.comment-mention')];
		expect(chips.map(chip => chip.textContent)).toEqual(['@Sam']);
		expect(chips[0]!.dataset.personTag).toBe('1');
		expect(chips[0]!.dataset.agentId).toBeUndefined();
	});

	it('marks a person tag beside agent tags, and shows no chip for a person who was not tagged', () => {
		vi.stubGlobal('ResizeObserver', ResizeObserverStub);
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(CommentMarkdownBody, {
			target: host,
			props: { text: 'Needs your call', taggedAgents: [{ id: 'ag1', name: 'Quality Gate' }], taggedPerson: 'Sam' }
		});
		flushSync();
		expect([...host.querySelectorAll<HTMLElement>('.comment-mention')].map(chip => chip.textContent)).toEqual(['@Quality Gate', '@Sam']);
		unmount(component); component = null;

		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(CommentMarkdownBody, { target: host, props: { text: 'Needs your call', taggedAgents: [{ id: 'ag1', name: 'Quality Gate' }] } });
		flushSync();
		expect([...host.querySelectorAll<HTMLElement>('.comment-mention')].map(chip => chip.textContent)).toEqual(['@Quality Gate']);
		expect(host.textContent).not.toContain('@ui');
	});
});
