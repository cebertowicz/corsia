import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { activeThreadId, messages } from '$lib/stores/threads';
import { handleWsError } from '$lib/ws/handlersConnection';
import { chatReplyDrafts } from './chatReplyDrafts.svelte';
import Host from './ChatCommentSelectionPickHost.test.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const anchor = { from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: ' more' };
function setup(send: () => string | void = () => 'input-1') {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('innerWidth', 1440);
	Object.defineProperty(Range.prototype, 'getClientRects', { value: () => [{ top: 120, bottom: 140, left: 10, right: 60 }], configurable: true });
	host = document.createElement('div');
	host.innerHTML = '<div class="messages"><div class="message assistant" data-message-id="m1"><div class="long-message-body">text more</div></div></div><div id="mount"></div>';
	document.body.append(host);
	activeThreadId.set('thread');
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [] }] as never);
	component = mount(Host, { target: host.querySelector('#mount')!, props: { onSendReply: send } });
}
async function openBox() {
	await tick();
	const range = document.createRange();
	range.setStart(host.querySelector('.long-message-body')!.firstChild!, 0);
	range.setEnd(host.querySelector('.long-message-body')!.firstChild!, 4);
	document.getSelection()!.removeAllRanges(); document.getSelection()!.addRange(range);
	document.dispatchEvent(new Event('selectionchange')); flushSync();
	host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync(); await tick(); flushSync();
}
async function post(text = 'Keep my comment') {
	const field = host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	field.value = text; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	host.querySelector<HTMLButtonElement>('[role="dialog"] button.submit')!.click();
	await tick(); flushSync();
}
function reject(inputId = 'input-1') {
	handleWsError({ thread_id: 'thread', input_id: inputId, message: 'The selected text changed.' });
	flushSync();
}
afterEach(() => {
	if (component) unmount(component as never);
	component = null; messages.set([]); activeThreadId.set(null); host?.remove();
	chatReplyDrafts.setRoot('thread', 'm1', '');
	Reflect.deleteProperty(Range.prototype, 'getClientRects'); vi.unstubAllGlobals();
});

it('keeps text pending, then shows rejection and retries until admission clears it', async () => {
	setup(vi.fn().mockReturnValueOnce('input-1').mockReturnValueOnce('input-2'));
	await openBox(); await post();
	expect(host.querySelector<HTMLButtonElement>('button.submit')?.disabled).toBe(true);
	reject(); await tick(); flushSync();
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('selected text changed');
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('Keep my comment');
	host.querySelector<HTMLButtonElement>('button.submit')!.click(); await tick(); flushSync();
	expect(chatReplyDrafts.root('thread', 'm1')?.inputId).toBe('input-2');
	chatReplyDrafts.settle('thread', 'input-2'); await tick(); flushSync();
	expect(chatReplyDrafts.root('thread', 'm1')).toBeUndefined();
});

it('reopens a closed box on the rendered target when rejection arrives', async () => {
	setup(); await openBox(); await post();
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await tick(); flushSync();
	expect(host.querySelector('[role="dialog"] textarea')).toBeNull();
	reject(); await tick(); flushSync();
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('Keep my comment');
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('selected text changed');
});

it('keeps a rejected draft when the target is not rendered and the user returns', () => {
	setup();
	chatReplyDrafts.postRoot('thread', 'm1', 'keep this', 'other-input', anchor, 1);
	activeThreadId.set('other'); host.querySelector('.message')!.remove();
	reject('other-input');
	expect(chatReplyDrafts.root('thread', 'm1')).toMatchObject({ text: 'keep this', error: 'The selected text changed.' });
});

it('keeps the text with an immediate socket failure', async () => {
	setup(() => { throw new Error('socket closed'); }); await openBox(); await post();
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('Connection lost');
	expect(chatReplyDrafts.root('thread', 'm1')?.text).toBe('Keep my comment');
});
