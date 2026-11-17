import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatCommentSelectionPickHost from './ChatCommentSelectionPickHost.test.svelte';
import { messages } from '$lib/stores/threads';

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
