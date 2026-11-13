import type { CommentThreadData } from './CommentThreadData';

/** Select the latest unread item when a person opens a document thread. */
export function createThreadOpen(comment: () => CommentThreadData, chat: () => boolean, stamp: (id: string) => void) {
	function openThread() {
		if (chat()) return;
		const current = comment();
		const unread = new Set(current.newReplyIds ?? []);
		const last = [...current.replies].reverse().find((reply) => reply.id && unread.has(reply.id));
		if (last?.id) stamp(last.id);
		else if (current.newComment) stamp(current.id);
	}
	function openFromFocus(event: FocusEvent) {
		if ((event.target as Element).closest('textarea')) openThread();
	}
	return { openThread, openFromFocus };
}

/**
 * The replies a mounted thread treats as new: each one the server named since the thread was last at rest. Opening the
 * thread stamps it viewed and the server stops naming them, but the reader still sees which replies were new, and the
 * collapse point stays put, until the thread rests again (a margin card goes back to its preview), so a later pick
 * starts from what is unread then. The caller reads this while resting too, or a rest in between would go unseen.
 */
export function createOpenedReplies() {
	const seen = new Set<string>();
	return (newReplyIds: readonly string[] | undefined, resting: boolean): Set<string> => {
		if (resting) seen.clear();
		for (const id of newReplyIds ?? []) seen.add(id);
		return new Set(seen);
	};
}
