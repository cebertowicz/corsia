import type { AttentionQueue, CommentReplyItem } from '$lib/api/attentionQueue';

/** Passive replies for the current chat, separate from Needs you. */
export function replyItemsForThread(queue: AttentionQueue, threadId: string | null): CommentReplyItem[] {
	return (queue.reply_items ?? []).filter((item) => item.detail.source.thread_id === threadId);
}
