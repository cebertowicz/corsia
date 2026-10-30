import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread, { type CommentThreadData } from './CommentThread.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const comment: CommentThreadData = { id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] };

function render() {
	host = document.createElement('div'); document.body.appendChild(host);
	const onReply = vi.fn().mockResolvedValue(undefined);
	component = mount(CommentThread, { target: host, props: { comment, onReply, flat: true } });
	flushSync();
	return { onReply };
}
function openReply() {
	return host.querySelector<HTMLTextAreaElement>('.ct-input')!;
}
function type(input: HTMLTextAreaElement, value: string) {
	input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
}
function press(input: HTMLTextAreaElement, init: KeyboardEventInit) {
	const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ...init });
	input.dispatchEvent(event); flushSync();
	return event;
}
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); vi.unstubAllGlobals(); });

describe('CommentThread reply field', () => {
	it('replies to an opened resolved thread without losing focus or caret', async () => {
		host = document.createElement('div'); document.body.appendChild(host);
		const onReply = vi.fn().mockResolvedValue(undefined);
		component = mount(CommentThread, { target: host, props: { comment: { ...comment, resolved: true }, onReply, flat: true } });
		flushSync();
		host.querySelector<HTMLButtonElement>('.ct-res-head')!.click(); flushSync();
		const input = openReply(); expect(input).toBeTruthy(); input.focus();
		type(input, 'Hello'); input.setSelectionRange(2, 2);
		expect(document.activeElement).toBe(input);
		expect(input.selectionStart).toBe(2);
		press(input, {});
		await vi.waitFor(() => expect(onReply).toHaveBeenCalledWith('c1', 'Hello'));
		expect(host.querySelector('.ct-res-head')).toBeTruthy();
	});

	it('hides the reply field on an opened read-only resolved thread', () => {
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(CommentThread, { target: host, props: { comment: { ...comment, resolved: true }, readOnly: true, flat: true } });
		flushSync(); host.querySelector<HTMLButtonElement>('.ct-res-head')!.click(); flushSync();
		expect(host.querySelector('.ct-input')).toBeNull();
	});

	it('is a textarea: a long reply wraps instead of scrolling sideways', () => {
		render();
		expect(openReply().tagName).toBe('TEXTAREA');
	});

	it('Enter sends, Shift+Enter keeps the new line', () => {
		const { onReply } = render();
		const input = openReply();
		type(input, 'Looks right');
		expect(press(input, { shiftKey: true }).defaultPrevented).toBe(false);
		expect(onReply).not.toHaveBeenCalled();
		expect(press(input, {}).defaultPrevented).toBe(true);
		expect(onReply).toHaveBeenCalledWith('c1', 'Looks right');
	});

	it('on a touch screen Enter is a new line and Cmd+Enter sends', () => {
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('coarse'), addEventListener() {}, removeEventListener() {} }));
		const { onReply } = render();
		const input = openReply();
		type(input, 'Looks right');
		expect(press(input, {}).defaultPrevented).toBe(false);
		expect(onReply).not.toHaveBeenCalled();
		press(input, { metaKey: true });
		expect(onReply).toHaveBeenCalledWith('c1', 'Looks right');
	});

	it('shrinks back to one line once a multi-line reply is sent', async () => {
		// jsdom has no layout: read a line count off the value, as a browser would for a collapsed field.
		const real = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'scrollHeight');
		Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', { configurable: true, get(this: HTMLTextAreaElement) { return 20 * Math.max(1, this.value.split('\n').length); } });
		try {
			render();
			const input = openReply();
			type(input, 'one\ntwo\nthree\nfour');
			expect(input.style.height).toBe('80px');
			press(input, {});
			await vi.waitFor(() => expect(input.value).toBe(''));
			expect(input.style.height).toBe('20px');
		} finally {
			if (real) Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', real);
			else delete (HTMLTextAreaElement.prototype as { scrollHeight?: number }).scrollHeight;
		}
	});
});
