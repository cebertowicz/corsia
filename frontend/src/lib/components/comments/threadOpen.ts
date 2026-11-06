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
