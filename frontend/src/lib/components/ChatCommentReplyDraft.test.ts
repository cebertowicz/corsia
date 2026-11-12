import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { activeThreadId, messages, type ChatComment } from '$lib/stores/threads';

import ChatCommentThreads from './ChatCommentThreads.svelte';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
import { CommentSendRefused } from '$lib/chatCommentAnchors';

const comment = { id: 'root', cid: 'c1', from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', user_message_id: 'sender', created_at: '2026-10-27T00:00:00Z', note: 'Why?', quote: 'selected', delivery_state: 'delivered', answers: [] } as ChatComment;
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const send = vi.fn();
function show(comments: ChatComment[] = [comment]) {
	host = document.createElement('div'); document.body.append(host);
	component = mount(ChatCommentThreads, { target: host, props: { messageId: 'target', threadId: 'thread', comments, onReply: send, keepReplyVisible: true } });
	flushSync();
}
async function settle() { for (let i = 0; i < 3; i++) { await tick(); flushSync(); } }
afterEach(() => { if (component) unmount(component as never); component = null; host?.remove(); chatCommentDrafts.set('root', ''); activeThreadId.set(null); messages.set([]); send.mockReset(); vi.unstubAllGlobals(); });

it('keeps a refused reply across a thread switch, then clears it once a retry is stored', async () => {
	let refuse!: (error: unknown) => void;
	send.mockImplementationOnce(() => new Promise<void>((_, no) => { refuse = no; })).mockImplementationOnce(() => Promise.resolve());
	activeThreadId.set('thread'); show();
	const field = host.querySelector<HTMLTextAreaElement>('textarea')!;
	field.value = 'keep my reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	host.querySelector<HTMLButtonElement>('[data-send-reply]')!.click(); await settle();
	expect(chatCommentDrafts.reply('root')?.posting).toBe(true);
	unmount(component! as never); component = null; host.remove(); activeThreadId.set('other');
	refuse(new CommentSendRefused('Please try again.')); await settle();
	expect(chatCommentDrafts.get('root')).toBe('keep my reply');
	activeThreadId.set('thread'); show();
	expect(host.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('keep my reply');
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('Please try again');
	host.querySelector<HTMLButtonElement>('[data-send-reply]')!.click(); await settle();
	expect(send).toHaveBeenCalledTimes(2);
	expect(chatCommentDrafts.get('root')).toBe('');
});

// A reply the server stored and then could not deliver is on the comment, with the server's delivery state.
const failedReply = { ...comment, answers: [{ author_kind: 'user', user_message_id: 'stored-1', cid: 'r1', text: 'keep my reply', created_at: '2026-10-27T00:00:01Z', unread: false, delivery_state: 'failed', delivery_error: 'Delivery failed.' }] } as ChatComment;

it('keeps a reply that was stored and then failed, with its Retry, in its thread and not as a delivered reply', () => {
	activeThreadId.set('thread'); show([failedReply]);
	const failure = host.querySelector('.failed-reply');
	expect(failure?.textContent).toContain('keep my reply');
	expect(failure?.textContent).toContain('Not delivered');
	expect([...(failure?.querySelectorAll('button') ?? [])].map(button => button.textContent)).toContain('Retry');
	expect(host.textContent?.split('keep my reply')).toHaveLength(2);
});

it('shows a comment whose own input failed as not delivered, with Retry', () => {
	activeThreadId.set('thread'); show([{ ...comment, delivery_state: 'failed', delivery_error: 'Delivery failed.' }]);
	expect(host.querySelector('.failed-reply')?.textContent).toContain('Comment not delivered');
});

// Retry answers with the canonical input row. That row is the comment's, not chat's: it must not reach the transcript.
it('retries a failed reply without putting its row in the transcript', async () => {
	// The chat on screen, so the retry's answer would be adopted if its row were there.
	activeThreadId.set('thread');
	messages.set([{ id: 'target', thread_id: 'thread', role: 'assistant', content: 'Answer', seq: 1, created_at: '2026-10-27T00:00:00Z', chat_comments: [failedReply] }] as never);
	const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ thread_id: 'thread', message: { id: 'stored-1', thread_id: 'thread', role: 'user', content: '', delivery_state: 'queued', metadata: JSON.stringify({ reply_context: { comments: [{ note: 'keep my reply' }] } }) } }) }));
	vi.stubGlobal('fetch', fetchMock);
	show([failedReply]);
	[...host.querySelectorAll<HTMLButtonElement>('.failed-reply button')].find(button => button.textContent === 'Retry')!.click();
	await settle();
	expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toContain('/api/threads/thread/messages/stored-1/retry');
	expect(get(messages).map(row => [row.id, row.role])).toEqual([['target', 'assistant']]);
});
