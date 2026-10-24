import type { AttentionQueue, CommentReplyItem } from '$lib/api/attentionQueue';
import { on } from '$lib/ws';

/** Subscribe only to comment changes that can alter reply attention. */
export function subscribeReplyAttention(reload: () => void): () => void {
	return on('doc_comment_changed', (frame: unknown) => {
		if (typeof frame === 'object' && frame !== null && 'reply_attention_changed' in frame && frame.reply_attention_changed === true) reload();
	});
}

/** Passive replies for the current chat, separate from Needs you. */
export function replyItemsForThread(queue: AttentionQueue, threadId: string | null): CommentReplyItem[] {
	return (queue.reply_items ?? []).filter((item) => item.detail.source.thread_id === threadId);
}
