import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import ChatCommentSelectionPickHost from './ChatCommentSelectionPickHost.test.svelte';
import ChatCommentReadHost from './ChatCommentReadHost.test.svelte';
import { messages } from '$lib/stores/threads';
import { pickedComments } from '$lib/stores/docComments';

// The chat counterpart of DocViewer.unreadTint.test.ts: a thread with the person and the agent answering in turns, column up.

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function setupSurface(): void {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('innerWidth', 1440);
	host = document.createElement('div');
	host.innerHTML = '<div class="messages"><div class="message assistant" data-message-id="m1"><div class="long-message-body">text more</div></div></div><div id="mount"></div>';
	document.body.append(host);
}

const answer = (seq: number, author_kind: 'agent' | 'user', unread: boolean) => ({ user_message_id: 'u1', cid: 'c1', assistant_message_id: `a${seq}`, author_kind, text: `turn ${seq}`, created_at: `2026-11-16T01:5${seq}:00Z`, unread });
const comment = (unread: boolean) => ({ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-16T01:50:00Z',
	answers: [answer(1, 'agent', false), answer(2, 'user', false), answer(3, 'agent', unread)] });
const show = (unread: boolean) => messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [comment(unread)] }] as never);
const tinted = () => host.querySelector('.long-message-body [data-comment-ids="one"][data-unread]') !== null;

afterEach(() => {
	if (component) unmount(component as never);
	component = null;
	messages.set([]);
	// A pick no card consumed (the pick host draws none) would be read by the next test's card on mount.
	pickedComments.set(new Set());
	host?.remove();
	vi.unstubAllGlobals();
});

it('drops the unread tint once the thread is read and its picked card is let go', async () => {
	setupSurface();
	show(true);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(tinted()).toBe(true));
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	show(false); // the stamp landed
	flushSync();
	await tick();
	expect(tinted(), 'held while its card is open').toBe(true);
	document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
	flushSync();
	await tick();
	await vi.waitFor(() => expect(tinted(), 'a read thread whose card is let go still has the unread tint').toBe(false));
});

// A thread with several turns: the person replied twice, and the agent's three unread answers sit on three different
// comment rows, as the server sends them (`attach_comments`): one on the comment, one on the first reply, and one
// message answering both replies, which is read through its earliest tagged row, the first reply.
const turns = (unread: boolean) => ({ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-16T01:50:00Z',
	answers: [
		{ author_kind: 'agent', user_message_id: 'u1', cid: 'c1', assistant_message_id: 'a1', text: 'turn 1', created_at: '2026-11-16T01:51:00Z', unread },
		{ author_kind: 'user', user_message_id: 'u2', cid: 'c2', text: 'turn 2', created_at: '2026-11-16T01:52:00Z', unread: false },
		{ author_kind: 'agent', user_message_id: 'u2', cid: 'c2', assistant_message_id: 'a3', text: 'turn 3', created_at: '2026-11-16T01:53:00Z', unread },
		{ author_kind: 'user', user_message_id: 'u3', cid: 'c3', text: 'turn 4', created_at: '2026-11-16T01:54:00Z', unread: false },
		{ author_kind: 'agent', user_message_id: 'u2', cid: 'c2', assistant_message_id: 'a5', text: 'turn 5', created_at: '2026-11-16T01:55:00Z', unread },
	] });

it('reads every unread answer of a thread with several turns when its card is picked, and drops New and the tint once it is let go', async () => {
	setupSurface();
	const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
	vi.stubGlobal('fetch', fetcher);
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [turns(true)] }] as never);
	component = mount(ChatCommentReadHost, { target: host.querySelector('#mount')! });
	const cardNew = () => host.querySelector('[data-card="one"] .ct-head .ct-new') !== null;
	await vi.waitFor(() => expect(tinted()).toBe(true));
	expect(cardNew(), 'the resting card names its unread answers').toBe(true);
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	expect(host.querySelector<HTMLElement>('[data-picked]')!.dataset.picked).toBe('one');
	await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
	expect(fetcher.mock.calls.map(([url, init]) => [String(url).replace(/^.*\/messages\//, ''), JSON.parse(init.body).assistant_message_id])).toEqual([
		['u1/comments/c1/viewed', 'a1'], ['u2/comments/c2/viewed', 'a3'], ['u2/comments/c2/viewed', 'a5'],
	]);
	await vi.waitFor(() => expect(get(messages)[0].chat_comments![0].answers.filter(answer => answer.unread)).toEqual([]));
	expect(tinted(), 'held while its card is open').toBe(true);
	document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
	flushSync();
	await tick();
	expect(host.querySelector<HTMLElement>('[data-picked]')!.dataset.picked).toBe('');
	await vi.waitFor(() => expect(tinted(), 'a read thread whose card is let go still has the unread tint').toBe(false));
	expect(cardNew(), 'a read thread whose card is let go still says New').toBe(false);
	delete (document as unknown as Record<string, unknown>).visibilityState;
});

it('drops the unread tint when the thread was read elsewhere and its card was never opened', async () => {
	setupSurface();
	show(true);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(tinted()).toBe(true));
	show(false);
	flushSync();
	await tick();
	await vi.waitFor(() => expect(tinted()).toBe(false));
});
