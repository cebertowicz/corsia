import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { activeThreadId, messages } from '$lib/stores/threads';
import { handleWsError } from '$lib/ws/handlersConnection';
import { handleMessage } from '$lib/ws/handlers';
import { chatCommentInputs } from '$lib/chatCommentInputs.svelte';
import { emit } from '$lib/ws/events';
import { postChatComment } from './composerSend';
import { CommentSendRefused, type ChatComment } from '$lib/chatCommentAnchors';
import { setWs } from '$lib/ws/state';
import { toasts } from '$lib/stores/toast';
import Host from './ChatCommentSelectionPickHost.test.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let lastInput: string | null = null;
const anchor = { from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: ' more' };
const second = { from_off: 5, to_off: 9, quote: 'more', anchor_prefix: 'text ', anchor_suffix: '' };
function setup(send: (reply: ChatComment) => string | void = () => 'input-1') {
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
	// The composer's send names the comment it carried to the store, as postChatComment does.
	const onSendReply = (reply: ChatComment) => {
		const inputId = send(reply);
		if (inputId) { lastInput = inputId; chatCommentInputs.sending(inputId, 'thread', reply as never); }
		return inputId;
	};
	component = mount(Host, { target: host.querySelector('#mount')!, props: { onSendReply } });
}
async function openBox(on: { from_off: number; to_off: number } = anchor) {
	await tick();
	// A painted highlight splits the body's text, so offsets are found across its text nodes.
	const range = document.createRange();
	const walker = document.createTreeWalker(host.querySelector('.long-message-body')!, NodeFilter.SHOW_TEXT);
	for (let node = walker.nextNode() as Text | null, at = 0; node; at += node.length, node = walker.nextNode() as Text | null) {
		if (on.from_off >= at && on.from_off <= at + node.length) range.setStart(node, on.from_off - at);
		if (on.to_off >= at && on.to_off <= at + node.length) { range.setEnd(node, on.to_off - at); break; }
	}
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
	// A refusal frees any box still waiting, so Cancel below can drop it.
	for (const id of ['input-1', 'input-2', 'other-input']) emit('error', { input_id: id, message: 'reset' });
	setWs(null as unknown as WebSocket); for (const toast of get(toasts)) toasts.dismiss(toast.id);
	for (const on of [anchor, second]) chatCommentInputs.dropRoot('thread', 'm1', { ...on, quote: '', anchor_prefix: '', anchor_suffix: '' });
	Reflect.deleteProperty(Range.prototype, 'getClientRects'); vi.unstubAllGlobals();
});

it('keeps text pending, then shows rejection and retries until admission clears it', async () => {
	setup(vi.fn().mockReturnValueOnce('input-1').mockReturnValueOnce('input-2'));
	await openBox(); await post();
	expect(host.querySelector<HTMLButtonElement>('button.submit')?.disabled).toBe(true);
	reject(); await tick(); flushSync();
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('selected text changed');
	// The open box says it, so no toast says it again over the passage.
	expect(get(toasts)).toHaveLength(0);
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('Keep my comment');
	host.querySelector<HTMLButtonElement>('button.submit')!.click(); await tick(); flushSync();
	expect(chatCommentInputs.root('thread', 'm1', anchor)?.inputId).toBe('input-2');
	handleMessage({ type: 'input_queued', queue_kind: 'messages', thread_id: 'thread', input_id: 'input-2', message_id: 'input-2', delivery_state: 'queued' }, () => {});
	await tick(); flushSync();
	expect(chatCommentInputs.root('thread', 'm1', anchor)).toBeUndefined();
});

it('keeps a closed box\'s rejection and text for its passage, where reopening the box shows them', async () => {
	setup(); await openBox(); await post();
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await tick(); flushSync();
	expect(host.querySelector('[role="dialog"] textarea')).toBeNull();
	reject(); await tick(); flushSync();
	await openBox();
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('Keep my comment');
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('selected text changed');
});

it('keeps a rejected draft when the target is not rendered and the user returns', () => {
	setup();
	chatCommentInputs.sending('other-input', 'thread', { id: 'other', seq: 1, ...anchor, note: 'keep this' });
	chatCommentInputs.postRoot('thread', 'm1', anchor, 'keep this', 'other-input', '');
	activeThreadId.set('other'); host.querySelector('.message')!.remove();
	reject('other-input');
	expect(chatCommentInputs.root('thread', 'm1', anchor)).toMatchObject({ text: 'keep this', error: 'The selected text changed.' });
});

it('keeps the text with an immediate socket failure', async () => {
	setup(() => { throw new Error('socket closed'); }); await openBox(); await post();
	expect(host.querySelector('[role="alert"]')?.textContent).toBe('Couldn’t send this comment. Try again.');
	expect(host.querySelector<HTMLButtonElement>('button.submit')?.textContent).toBe('Retry');
	expect(chatCommentInputs.root('thread', 'm1', anchor)?.text).toBe('Keep my comment');
});

it('shows a refused send\'s own cause, the sentence its toast shows', async () => {
	setup(() => { throw new CommentSendRefused('Couldn’t send this comment because CORSIA is restarting after an update. Your comment stays here; try again once CORSIA is back.'); });
	await openBox(); await post();
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('CORSIA is restarting after an update');
});

it('keeps one passage\'s text and failure off another passage of the same message', async () => {
	setup(() => { throw new Error('socket closed'); }); await openBox(); await post('On the first passage');
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await tick(); flushSync();
	await openBox(second);
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('');
	expect(host.querySelector('[role="alert"]')).toBeNull();
	expect(host.querySelector<HTMLButtonElement>('button.submit')?.textContent).toBe('Comment');
});

it('keeps the passage\'s text when Escape closes the box', async () => {
	setup(); await openBox();
	const field = host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	field.value = 'Typed, then Escape'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	field.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await tick(); flushSync();
	expect(chatCommentInputs.root('thread', 'm1', anchor)?.text).toBe('Typed, then Escape');
	await openBox();
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('Typed, then Escape');
});

it('lets Cancel drop the passage\'s text and failure', async () => {
	setup(() => { throw new Error('socket closed'); }); await openBox(); await post();
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await tick(); flushSync();
	expect(chatCommentInputs.root('thread', 'm1', anchor)).toBeUndefined();
	await openBox();
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('');
	expect(host.querySelector('[role="alert"]')).toBeNull();
});

it('closes only its own box when an earlier post is stored', async () => {
	setup(); await openBox(); await post('First passage');
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await tick(); flushSync();
	await openBox(second);
	const field = host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	expect(field.value).toBe('');
	expect(field.readOnly).toBe(false);
	field.value = 'Second passage'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	handleMessage({ type: 'input_queued', queue_kind: 'messages', thread_id: 'thread', input_id: 'input-1', message_id: 'input-1', delivery_state: 'queued' }, () => {});
	await tick(); flushSync();
	expect(host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('Second passage');
});

it('closes the box once the post is stored', async () => {
	setup(); await openBox(); await post();
	handleMessage({ type: 'input_queued', queue_kind: 'messages', thread_id: 'thread', input_id: 'input-1', message_id: 'input-1', delivery_state: 'queued' }, () => {});
	await tick(); flushSync(); await tick(); flushSync();
	expect(host.querySelector('[role="dialog"] textarea')).toBeNull();
	expect(chatCommentInputs.root('thread', 'm1', anchor)).toBeUndefined();
});

it('keeps a comment stored and then failed on its own row, with its text, for its thread', async () => {
	setWs({ readyState: WebSocket.OPEN, send: () => {} } as unknown as WebSocket);
	setup(reply => postChatComment(reply, 'thread', undefined, false, true));
	await openBox(); await post();
	const inputId = lastInput!;
	// No transcript row stands for it, before or after the server stores it.
	expect(get(messages).some(row => row.role === 'user')).toBe(false);
	handleMessage({ type: 'input_queued', queue_kind: 'messages', thread_id: 'thread', input_id: inputId, message_id: 'stored-1', delivery_state: 'failed' }, () => {});
	await tick(); flushSync(); await tick(); flushSync();
	expect(host.querySelector('[role="dialog"] textarea')).toBeNull();
	// The server's row is what the comment's thread reads its failure and Retry from.
	expect(get(messages).some(row => row.role === 'user')).toBe(false);
	expect(chatCommentInputs.rows('thread').find(row => row.id === 'stored-1')).toMatchObject({ delivery_state: 'failed', metadata: { reply_context: { comments: [{ note: 'Keep my comment' }] } } });
});
