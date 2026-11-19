import type { CommentThreadData } from './CommentThreadData';

/**
 * A press that is not an engagement with the thread it lands in: one on a link in the body, or one that ends a text selection.
 * Every card that reads on a press checks this first, so a link or a selection reads on no surface.
 */
export function notEngaging(event: Event): boolean {
	return !!(event.target as Element | null)?.closest?.('a') || !(window.getSelection()?.isCollapsed ?? true);
}

/**
 * The one place a thread decides it is read. `engage` is every way the person engages with it: a press anywhere on its card
 * (its number disc and controls included), a press on its highlight, marker, image tag or jump control (a pick), opening it
 * from a list, sheet, popover or drawer, or moving focus into its reply field. Each reads what is unread at that moment,
 * so a reply that arrived while the thread stayed open is read by the next engagement.
 * Being on screen reads nothing, and neither does focus the thread puts in its own reply field (`focusQuietly`): that
 * follows a selection, which may be a restored tab's landing rather than a pick.
 * `close` (the thread rests again or leaves the page) reads it once more, only if it was engaged, so a reply that arrived
 * while it was open is read when the person lets it go; closing a thread nobody engaged with reads nothing.
 * Only the caller's pick says whether the thread is open, so `onEngaged` tells the caller of each engagement: one that leaves
 * the thread at rest (a press on a card the pick does not move to, such as an unplaced or resolved one) reads it and is
 * then closed at once, rather than held until the thread leaves the page.
 * A chat thread stamps each unread answer; a document thread stamps its latest unread item.
 */
export function createThreadEngagement(comment: () => CommentThreadData, chat: () => boolean, stamp: (id: string) => void, onEngaged: () => void = () => {}) {
	let quiet = false;
	let engaged = false;
	function engage() {
		engaged = true;
		readUnread();
		onEngaged();
	}
	function close() {
		if (!engaged) return;
		engaged = false;
		readUnread();
	}
	function readUnread() {
		const current = comment();
		const unread = new Set(current.newReplyIds ?? []);
		if (chat()) { for (const id of unread) stamp(id); return; }
		const last = [...current.replies].reverse().find((reply) => reply.id && unread.has(reply.id));
		if (last?.id) stamp(last.id);
		else if (current.newComment) stamp(current.id);
	}
	function openFromFocus(event: FocusEvent) {
		if (!quiet && (event.target as Element).closest('textarea')) engage();
	}
	/** Focus moved by the thread itself; `focusin` fires inside the call, so it is told apart there. */
	function focusQuietly(focus: () => void) {
		quiet = true;
		try { focus(); } finally { quiet = false; }
	}
	return { engage, openFromFocus, focusQuietly, close };
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
