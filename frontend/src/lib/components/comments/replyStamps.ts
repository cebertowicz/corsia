import type { CommentThreadData } from './CommentThreadData';

// Chat answers retain their visible dwell; document threads stamp when the person opens the card.
const VIEWED_DWELL_MS = 1_500;

/** Record which of a mounted thread's replies the person has seen: chat answers one by one after their dwell, document threads as a watermark. */
export function createReplyStamps(comment: () => CommentThreadData, chat: () => boolean, onViewed: () => ((commentId: string, replyId: string) => Promise<void>) | undefined) {
	// Plain variable: the newest reply this mounted thread has stamped, so a repeat callback cannot stamp it twice.
	let lastStamped: string | null = null;
	const stampedReplies = new Set<string>();
	const dwellTimers = new Map<string, number>();
	const dwelled = new Set<string>();
	let stampTimer: number | null = null;
	function cancelDwell(replyId?: string) {
		for (const [id, timer] of dwellTimers) {
			if (!replyId || id === replyId) { window.clearTimeout(timer); dwellTimers.delete(id); }
		}
		if (!replyId && stampTimer !== null) { window.clearTimeout(stampTimer); stampTimer = null; }
	}
	// One reply's own observer calls this with that reply's id, so nothing carries over from another element.
	function stamp(replyId: string) {
		const current = comment();
		const record = onViewed();
		if (!record || document.visibilityState !== 'visible' || (!current.newComment && !current.newReplyIds?.length)) return;
		if (chat()) {
			if (stampedReplies.has(replyId)) return;
			stampedReplies.add(replyId);
			record(current.id, replyId).catch((err) => {
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
	function advanceStamp() {
		stampTimer = null;
		const current = comment();
		const unread = new Set(current.newReplyIds ?? []);
		if (chat()) {
			for (const replyId of unread) if (dwelled.has(replyId)) stamp(replyId);
			return;
		}
		let through: string | undefined;
		for (const reply of current.replies) {
			if (!reply.id || !unread.has(reply.id)) continue;
			if (!dwelled.has(reply.id)) break;
			through = reply.id;
		}
		if (through) stamp(through);
	}
	function dwellThenStamp(replyId: string) {
		if (document.visibilityState !== 'visible' || dwellTimers.has(replyId)) return;
		if (dwelled.has(replyId)) {
			if (stampTimer === null) stampTimer = window.setTimeout(advanceStamp, 0);
			return;
		}
		dwellTimers.set(replyId, window.setTimeout(() => {
			dwellTimers.delete(replyId);
			dwelled.add(replyId);
			// Coalesce replies that finish their dwell together into one watermark update.
			if (stampTimer === null) stampTimer = window.setTimeout(advanceStamp, 0);
		}, VIEWED_DWELL_MS));
	}
	return { stamp, dwellThenStamp, cancelDwell };
}
