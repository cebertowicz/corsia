import { get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { token } from '$lib/stores/auth';
import { activeThreadId } from '$lib/stores/threads';
import { beginChatReplyAttentionRead, chatReplyAttention, refreshChatReplyAttention, setChatReplyAttention } from './chatReplyAttention';
import type { CommentReplyItem } from '$lib/api/attentionQueue';

const item = (id: string, unread = 1): CommentReplyItem => ({ detail: { title: '', unread_count: unread, newest_at: 't', destination: { pane: 'chat-replies', id, user_message_id: 'sender', cid: 'c1.1', seq: 1 }, source: { thread_id: 'thread' } } });
afterEach(() => { vi.unstubAllGlobals(); chatReplyAttention.set(null); activeThreadId.set(null); token.set(null); });

it('discards a late older-page snapshot after a newer refresh', async () => {
	activeThreadId.set('thread');
	token.set('bearer');
	const olderPage = beginChatReplyAttentionRead();
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ chat_reply_items: [item('new', 2)] }) })));
	await refreshChatReplyAttention('thread');
	setChatReplyAttention('thread', [item('old', 4)], olderPage);
	expect(get(chatReplyAttention)?.items).toEqual([item('new', 2)]);
});

it('clears stale attention on an empty refresh and ignores a late page', async () => {
	activeThreadId.set('thread');
	token.set('bearer');
	setChatReplyAttention('thread', [item('old')], beginChatReplyAttentionRead());
	const olderPage = beginChatReplyAttentionRead();
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ chat_reply_items: [] }) })));
	await refreshChatReplyAttention('thread');
	setChatReplyAttention('thread', [item('old')], olderPage);
	expect(get(chatReplyAttention)?.items).toEqual([]);
});

it('rejects a response from before switching away and back to the same thread', () => {
	activeThreadId.set('thread');
	const oldRead = beginChatReplyAttentionRead();
	activeThreadId.set('other');
	activeThreadId.set('thread');
	setChatReplyAttention('thread', [item('old')], oldRead);
	expect(get(chatReplyAttention)).toBeNull();
});
