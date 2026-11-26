import type { DocComment } from '$lib/stores/docComments';

/**
 * The document's unread total: every unread reply plus each unread root comment, suggestion included. One unit for the
 * whole document surface, so the jump control's label, the pill and the dock's chip never disagree.
 */
export function unreadCommentCount(threads: DocComment[]): number {
	return threads.reduce((total, thread) => total + (thread.newReplyIds?.length ?? 0) + (thread.newComment ? 1 : 0), 0);
}

/**
 * The unread replies to the person's own comments: each unread reply in a thread the person
 * commented in. An unread top-level comment never counts — Activity's row and the open
 * document's bar speak only of "replies to your comments" and count these; the jump control
 * and document header keep counting every unread item (`unreadCommentCount`).
 */
export function unreadReplyCount(threads: DocComment[]): number {
	return threads.reduce((total, thread) => total + (
		thread.authorKind === 'user' || thread.replies.some((reply) => reply.authorKind === 'user')
			? thread.newReplyIds?.length ?? 0
			: 0), 0);
}
