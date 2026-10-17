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
});
