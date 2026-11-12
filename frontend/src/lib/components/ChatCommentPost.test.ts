import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { activeThreadId, messages } from '$lib/stores/threads';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
import { postChatComment } from './composerSend';
import { CommentSendRefused, type ChatComment } from '$lib/chatCommentAnchors';
import { toasts } from '$lib/stores/toast';
import Host from './ChatCommentSelectionPickHost.test.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const anchor = { from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: ' more' };
const second = { from_off: 5, to_off: 9, quote: 'more', anchor_prefix: 'text ', anchor_suffix: '' };
/** A post the test answers: the box waits on it as it waits on the server. */
function deferred() {
	let resolve!: () => void, reject!: (error: unknown) => void;
	const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
	return { promise, resolve, reject };
}
function setup(send: (reply: ChatComment) => Promise<void> = () => Promise.resolve()) {
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
async function settle() { for (let i = 0; i < 3; i++) { await tick(); flushSync(); } }
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
function type(text: string) {
	const field = host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	field.value = text; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
}
async function post(text = 'Keep my comment') {
	type(text);
	host.querySelector<HTMLButtonElement>('[role="dialog"] button.submit')!.click();
	await settle();
}
const box = () => host.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea');
afterEach(() => {
	if (component) unmount(component as never);
	component = null; messages.set([]); activeThreadId.set(null); host?.remove();
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	for (const on of [anchor, second]) chatCommentDrafts.dropRoot('thread', 'm1', { ...on, quote: '', anchor_prefix: '', anchor_suffix: '' });
	Reflect.deleteProperty(Range.prototype, 'getClientRects'); vi.unstubAllGlobals();
});

it('holds the text read-only while it posts, shows a refusal in the box, and closes once a retry is stored', async () => {
	const first = deferred(), again = deferred();
	setup(vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(again.promise));
	await openBox(); await post();
	expect(box()?.readOnly).toBe(true);
	expect(host.querySelector<HTMLButtonElement>('button.submit')?.disabled).toBe(true);
	first.reject(new CommentSendRefused('Couldn’t send this comment because the selected text changed.')); await settle();
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('selected text changed');
	// The open box says it, so no toast says it again over the passage.
	expect(get(toasts)).toHaveLength(0);
	expect(box()?.value).toBe('Keep my comment');
	host.querySelector<HTMLButtonElement>('button.submit')!.click(); await settle();
	expect(chatCommentDrafts.root('thread', 'm1', anchor)?.posting).toBe(true);
	again.resolve(); await settle();
	expect(chatCommentDrafts.root('thread', 'm1', anchor)).toBeUndefined();
	expect(box()).toBeNull();
});

it('keeps a closed box\'s refusal and text for its passage, where reopening the box shows them', async () => {
	const pending = deferred();
	setup(() => pending.promise); await openBox(); await post();
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await settle();
	expect(box()).toBeNull();
	pending.reject(new CommentSendRefused('Couldn’t send this comment because the selected text changed.')); await settle();
	await openBox();
	expect(box()?.value).toBe('Keep my comment');
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('selected text changed');
});

it('keeps a refused draft for its passage while another chat is showing', async () => {
	const pending = deferred();
	const stored = chatCommentDrafts.postRoot('thread', 'm1', anchor, 'keep this', () => pending.promise);
	activeThreadId.set('other');
	pending.reject(new CommentSendRefused('The selected text changed.'));
	expect(await stored).toBe(false);
	expect(chatCommentDrafts.root('thread', 'm1', anchor)).toMatchObject({ text: 'keep this', error: 'The selected text changed.' });
});

it('keeps the text with a failure nobody named', async () => {
	setup(() => Promise.reject(new Error('socket closed'))); await openBox(); await post();
	expect(host.querySelector('[role="alert"]')?.textContent).toBe('Couldn’t send this comment. Try again.');
	expect(host.querySelector<HTMLButtonElement>('button.submit')?.textContent).toBe('Retry');
	expect(chatCommentDrafts.root('thread', 'm1', anchor)?.text).toBe('Keep my comment');
});

it('shows a refused post\'s own cause', async () => {
	setup(() => Promise.reject(new CommentSendRefused('Couldn’t send this comment because CORSIA is restarting after an update. Your comment stays here; try again once CORSIA is back.')));
	await openBox(); await post();
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('CORSIA is restarting after an update');
});

it('keeps one passage\'s text and failure off another passage of the same message', async () => {
	setup(() => Promise.reject(new Error('socket closed'))); await openBox(); await post('On the first passage');
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await settle();
	await openBox(second);
	expect(box()?.value).toBe('');
	expect(host.querySelector('[role="alert"]')).toBeNull();
	expect(host.querySelector<HTMLButtonElement>('button.submit')?.textContent).toBe('Comment');
});

it('keeps the passage\'s text when Escape closes the box', async () => {
	setup(); await openBox();
	type('Typed, then Escape');
	box()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await settle();
	expect(chatCommentDrafts.root('thread', 'm1', anchor)?.text).toBe('Typed, then Escape');
	await openBox();
	expect(box()?.value).toBe('Typed, then Escape');
});

it('lets Cancel drop the passage\'s text and failure', async () => {
	setup(() => Promise.reject(new Error('socket closed'))); await openBox(); await post();
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await settle();
	expect(chatCommentDrafts.root('thread', 'm1', anchor)).toBeUndefined();
	await openBox();
	expect(box()?.value).toBe('');
	expect(host.querySelector('[role="alert"]')).toBeNull();
});

it('closes only its own box when an earlier post is stored', async () => {
	const first = deferred();
	setup(() => first.promise); await openBox(); await post('First passage');
	host.querySelector<HTMLButtonElement>('button.cancel')!.click(); await settle();
	await openBox(second);
	expect(box()?.value).toBe('');
	expect(box()?.readOnly).toBe(false);
	type('Second passage');
	first.resolve(); await settle();
	expect(box()?.value).toBe('Second passage');
});

// The real post: the server route stores it, and its answer is the comment's thread, on the row it is on.
it('posts to the chat comment route and shows the stored comment in its thread, never as a chat row', async () => {
	const stored = { id: 'c1', cid: 'cid-1', user_message_id: 'input-row', quote: 'text', note: 'Keep my comment', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: ' more', created_at: '2026-11-12T00:00:00Z', delivery_state: 'queued', delivery_error: null, answers: [] };
	const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ ok: true, json: async () => ({ message_id: 'input-row', delivery_state: 'queued', delivery_error: null, messages: [{ id: 'm1', role: 'assistant', chat_comments: [stored] }] }) }));
	setup(reply => postChatComment(reply, 'thread', false, true));
	vi.stubGlobal('fetch', fetchMock);
	await openBox(); await post();
	expect(fetchMock).toHaveBeenCalledTimes(1);
	const [url, init] = fetchMock.mock.calls[0];
	expect(String(url)).toBe('/api/threads/thread/chat-comments');
	const body = JSON.parse(String(init?.body));
	expect(body).toMatchObject({ comment: { seq: 1, from_off: 0, to_off: 4, quote: 'text', note: 'Keep my comment' } });
	expect(typeof body.input_id).toBe('string');
	await vi.waitFor(() => { flushSync(); expect(box()).toBeNull(); });
	expect(get(messages).map(row => [row.id, row.role])).toEqual([['m1', 'assistant']]);
	expect(get(messages)[0].chat_comments).toEqual([stored]);
});

it('keeps the text with the server\'s own sentence when the route refuses the post', async () => {
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: 'This chat is closed. Your comment stays here.' }) })));
	setup(reply => postChatComment(reply, 'thread', false, true));
	await openBox(); await post();
	expect(host.querySelector('[role="alert"]')?.textContent).toBe('This chat is closed. Your comment stays here.');
	expect(box()?.value).toBe('Keep my comment');
	expect(get(messages).map(row => row.id)).toEqual(['m1']);
});

it('sends the same request again when the first press got no answer, so the server stores one comment', async () => {
	const ok = { ok: true, json: async () => ({ messages: [] }) };
	const fetchMock = vi.fn().mockRejectedValueOnce(new TypeError('network down')).mockResolvedValueOnce(ok);
	vi.stubGlobal('fetch', fetchMock);
	setup(reply => postChatComment(reply, 'thread', false, true));
	await openBox(); await post('Sent twice, stored once');
	await vi.waitFor(() => { flushSync(); expect(host.querySelector<HTMLButtonElement>('button.submit')?.textContent).toBe('Retry'); });
	host.querySelector<HTMLButtonElement>('[role="dialog"] button.submit')!.click();
	await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
	const [first, again] = fetchMock.mock.calls.map(call => JSON.parse(String((call[1] as RequestInit).body)));
	expect(again).toEqual(first);
});
