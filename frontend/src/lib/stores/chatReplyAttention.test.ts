import { get } from 'svelte/store';
import { afterEach, expect, it } from 'vitest';
import { activeThreadId } from '$lib/stores/threads';
import { chatReplyAttention, mergeChatReplyAttention } from './chatReplyAttention';
import type { CommentReplyItem } from '$lib/api/attentionQueue';

const item = (id: string): CommentReplyItem => ({ detail: { title: '', unread_count: 1, newest_at: 't', destination: { pane: 'chat-replies', id, user_message_id: 'sender', cid: 'c1.1', seq: 1 }, source: { thread_id: 'thread' } } });
afterEach(() => { chatReplyAttention.set(null); activeThreadId.set(null); });

it('adds older-page replies without clearing the first page or duplicating its item', () => {
	activeThreadId.set('thread');
	chatReplyAttention.set({ threadId: 'thread', items: [item('first')] });
	mergeChatReplyAttention('thread', [item('first'), item('older')]);
	expect(get(chatReplyAttention)?.items.map(row => row.detail.destination.id)).toEqual(['first', 'older']);
	mergeChatReplyAttention('thread', []);
	expect(get(chatReplyAttention)?.items).toHaveLength(2);
});
