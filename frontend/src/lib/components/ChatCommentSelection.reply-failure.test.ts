import { get } from 'svelte/store';
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatCommentSelection from './ChatCommentSelection.svelte';
import { messages } from '$lib/stores/threads';
import { toasts } from '$lib/stores/toast';

// The thread holds the draft when a reply is refused, so the refusal has to reach the person. This walks
// the real surface: a painted highlight, the thread opened on it, a typed reply, and the send refused
// because the message the comment sits on no longer carries a place in history to answer into.

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

afterEach(() => {
	if (component) unmount(component as never);
	component = null;
	messages.set([]);
	host?.remove();
	vi.unstubAllGlobals();
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
});

it('says why a reply on a comment that left the chat is refused, and keeps the draft', async () => {
	setupSurface();
	// A row with no sequence: the server has no place in history to deliver the answer to.
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	component = mount(ChatCommentSelection, { target: host.querySelector('#mount')!, props: { threadId: 'thread', comments: [], onChange: vi.fn(), onSendReply: vi.fn() } });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	flushSync();
	const field = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea');
	expect(field).not.toBeNull();
	field!.value = 'keep this draft';
	field!.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	document.querySelector<HTMLButtonElement>('[role="dialog"] [data-send-reply]')!.click();
	await tick();
	expect(get(toasts).map(toast => toast.message)).toContain('CORSIA couldn’t send this reply because that comment is no longer in this chat. Comment on the text again.');
	expect(document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('keep this draft');
});
