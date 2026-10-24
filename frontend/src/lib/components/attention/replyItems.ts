import type { AttentionQueue, AttentionQueueItem, CommentReplyDetail } from '$lib/api/attentionQueue';

/** Passive replies for the current chat, separate from Needs you. */
export function replyItemsForThread(queue: AttentionQueue, threadId: string | null): (AttentionQueueItem & { detail: CommentReplyDetail })[] {
	return (queue.reply_items ?? queue.items).filter((item): item is AttentionQueueItem & { detail: CommentReplyDetail } =>
		item.renderer === 'comment-reply' && (item.detail as CommentReplyDetail).source.thread_id === threadId,
	);
}
