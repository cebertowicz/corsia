import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentMarkdownBody from './CommentMarkdownBody.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let resize: ResizeObserverCallback | undefined;
let observed: Element[] = [];
// Long by chat's one rule (over 900 characters), so a read body may collapse.
const LONG = 'A long comment body. '.repeat(50);

// Like the browser's, observe() refuses anything that is not an element.
class ResizeObserverStub {
	constructor(callback: ResizeObserverCallback) {
		resize = callback;
	}
	observe(target: unknown) {
		if (!(target instanceof Element)) throw new TypeError("Failed to execute 'observe' on 'ResizeObserver': parameter 1 is not of type 'Element'.");
		observed.push(target);
	}
	disconnect() {}
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	resize = undefined;
	observed = [];
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
		component = mount(CommentMarkdownBody, { target: host, props: { text: LONG } });
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

	it('watches the reveal box as well as the text, so a preview clamp that changes with the card is measured again', () => {
		vi.stubGlobal('ResizeObserver', ResizeObserverStub);
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(CommentMarkdownBody, { target: host, props: { text: 'A long comment body' } });
		flushSync();
		expect(observed).toEqual([host.querySelector('.ct-markdown'), host.querySelector('.ct-markdown-reveal')]);
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
		component = mount(CommentMarkdownBody, { target: inner, props: { text: LONG } });
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

	function mountMeasured(props: Record<string, unknown>) {
		vi.stubGlobal('ResizeObserver', ResizeObserverStub);
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
			callback(0);
			return 0;
		});
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(CommentMarkdownBody, { target: host, props: props as { text: string } });
		flushSync();
		const reveal = host.querySelector<HTMLElement>('.ct-markdown-reveal')!;
		Object.defineProperties(reveal, {
			clientHeight: { configurable: true, get: () => 64 },
			scrollHeight: { configurable: true, get: () => 320 }
		});
		Object.defineProperty(host.querySelector('.ct-markdown'), 'scrollHeight', { configurable: true, get: () => 320 });
		resize?.([], {} as ResizeObserver);
		flushSync();
		return reveal;
	}

	it('shows an unread long body whole, with no Show more', () => {
		const reveal = mountMeasured({ text: LONG, unread: true });
		expect(host.querySelector('.ct-more')).toBeNull();
		expect(reveal.classList.contains('whole')).toBe(true);
		expect(reveal.classList.contains('clamped')).toBe(false);
	});

	it('never collapses a read body that is tall but not long by the message rule', () => {
		const reveal = mountMeasured({ text: 'Six short lines\n\nof a comment\n\nthat wraps tall' });
		expect(host.querySelector('.ct-more')).toBeNull();
		expect(reveal.classList.contains('whole')).toBe(true);
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
	});
});
