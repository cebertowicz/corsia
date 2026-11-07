import type { DocComment } from '$lib/stores/docComments';

/**
 * The document's unread total: every unread reply plus each unread root comment, suggestion included. One unit for the
 * whole document surface, so the jump control's label, the pill and the dock's chip never disagree.
 */
export function unreadCommentCount(threads: DocComment[]): number {
	return threads.reduce((total, thread) => total + (thread.newReplyIds?.length ?? 0) + (thread.newComment ? 1 : 0), 0);
}
