import type { CommentThreadData } from './CommentThreadData';

/**
 * Record which of a mounted thread's replies the person has read. Only opening the thread reads it (`threadOpen`):
 * chat answers are stamped one by one, document threads as a watermark.
 */
export function createReplyStamps(comment: () => CommentThreadData, chat: () => boolean, onViewed: () => ((commentId: string, replyId: string) => Promise<void>) | undefined) {
	// Plain variable: the newest reply this mounted thread has stamped, so a repeat callback cannot stamp it twice.
	let lastStamped: string | null = null;
	const stampedReplies = new Set<string>();
	function stamp(replyId: string) {
		const current = comment();
		const record = onViewed();
		if (!record || document.visibilityState !== 'visible' || (!current.newComment && !current.newReplyIds?.length)) return;
		if (chat()) {
			if (stampedReplies.has(replyId)) return;
			stampedReplies.add(replyId);
			record(current.id, replyId).catch((err) => {
				// A failed stamp leaves the reply unread; the next opening retries it.
				stampedReplies.delete(replyId);
				console.warn('Could not record the viewed reply', err);
			});
			return;
		}
		const replyIndex = current.replies.findIndex((r) => r.id === replyId);
		if (replyId !== current.id && replyIndex < 0) return;
		const position = replyId === current.id ? 0 : replyIndex + 1;
		const lastPosition = lastStamped === current.id ? 0 : current.replies.findIndex((r) => r.id === lastStamped) + 1;
		if (lastStamped !== null && position <= lastPosition) return;
		const previous = lastStamped;
		lastStamped = replyId;
		record(current.id, replyId).catch((err) => {
			console.warn('Could not record the viewed reply', err);
			// A failed stamp leaves the marker; the next opening retries it.
			if (lastStamped === replyId) lastStamped = previous;
		});
	}
	return { stamp };
}
