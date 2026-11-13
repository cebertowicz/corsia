import type { CommentThreadData } from './CommentThreadData';

/**
 * Opening a thread is what reads it: a press on its card, the person moving focus into its reply field, or the caller
 * opening it (a picked margin card, a popover). Being on screen reads nothing, and neither does focus the thread puts in
 * its own reply field (`focusQuietly`): that follows a selection, which may be a restored tab's landing rather than a pick.
 * A chat thread stamps each unread answer; a document thread stamps its latest unread item.
 */
export function createThreadOpen(comment: () => CommentThreadData, chat: () => boolean, stamp: (id: string) => void) {
	let quiet = false;
	function openThread() {
		const current = comment();
		const unread = new Set(current.newReplyIds ?? []);
		if (chat()) { for (const id of unread) stamp(id); return; }
		const last = [...current.replies].reverse().find((reply) => reply.id && unread.has(reply.id));
		if (last?.id) stamp(last.id);
		else if (current.newComment) stamp(current.id);
	}
	function openFromFocus(event: FocusEvent) {
		if (!quiet && (event.target as Element).closest('textarea')) openThread();
	}
	/** Focus moved by the thread itself; `focusin` fires inside the call, so it is told apart there. */
	function focusQuietly(focus: () => void) {
		quiet = true;
		try { focus(); } finally { quiet = false; }
	}
	return { openThread, openFromFocus, focusQuietly };
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
