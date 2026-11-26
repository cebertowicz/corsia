// A chat's kept new comments, the first time the chat is drawn (`ChatCommentSelection`): every pressed one in it, new comment
// or reply, is tried now (`unsentComments`), and one box opens. A pressed draft opens before a typed one, then the one
// furthest down the chat (newest message, then latest passage). It opens on its passage; on a passage that no longer reads as
// kept it opens at its message on no text, with why, and goes nowhere on its own. The other typed drafts come back, as
// before, when their passage is selected.
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
import { chatTextPoint, type ChatCommentAnchor } from '$lib/chatCommentAnchors';
import { chatAnchorAt, chatPlaceHolds } from '$lib/keptPlace';
import { chatDraftOf, keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { commentFailure } from './composerSend';
import { messageBody } from './chatUnreadTargets';

/** The box that opens: on its message and passage, drawn at `rect`; `unplaced` when the passage no longer reads as kept. */
export type ResumedBox = { messageId: string; seq: number; anchor: ChatCommentAnchor; rect: DOMRect; unplaced?: true };
type Row = { id: string; seq?: number | null };

/** The box around a passage's lines, or null when it draws none. */
export function rangeRect(range: Range): DOMRect | null {
	const rects = [...range.getClientRects()];
	if (!rects.length) return null;
	const { top, bottom, left, right } = rects.reduce((box, rect) => ({
		top: Math.min(box.top, rect.top), bottom: Math.max(box.bottom, rect.bottom),
		left: Math.min(box.left, rect.left), right: Math.max(box.right, rect.right),
	}), { top: Infinity, bottom: -Infinity, left: Infinity, right: -Infinity });
	return new DOMRect(left, top, right - left, bottom - top);
}

/** The drawn body of a message in the transcript under `root`. */
export function bodyOf(root: ParentNode, messageId: string): HTMLElement | null {
	return messageBody(root.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(messageId)}"]`));
}

/**
 * A draft kept before its passage was (an older version) has only its offsets, which are exact in a sent message: they
 * hold while the message reaches them and, when its press kept the quote, still read that quote.
 */
function offsetsHold(text: string, at: { from_off: number; to_off: number }, quote: string | undefined): boolean {
	return at.to_off <= text.length && (quote === undefined || text.slice(at.from_off, at.to_off) === quote);
}

/** The one kept new comment in this chat whose box opens with it: pressed before typed, then the furthest down. */
function keptToOpen(root: HTMLElement, threadId: string, rows: Row[]) {
	const kept = keptCommentDrafts.keys().flatMap(key => {
		const at = chatDraftOf(key), draft = at?.threadId === threadId ? keptCommentDrafts.get(key) : undefined;
		const message = at && draft?.body.trim() ? rows.find(item => item.id === at.messageId) : undefined;
		const body = message ? bodyOf(root, message.id) : null;
		return at && draft && message && body && typeof message.seq === 'number' ? [{ key, at, draft, seq: message.seq, body, pressed: unsentComments.sending(key) }] : [];
	});
	return kept.sort((a, b) => Number(b.pressed) - Number(a.pressed) || b.seq - a.seq || b.at.from_off - a.at.from_off)[0];
}

/**
 * Bring back this chat's kept comments over its drawn transcript `root`: the pressed ones go, and the box to open is
 * returned, or null for none. With `boxFree` false (a box or a thread is open already) none opens, and the pressed still go.
 */
export function resumeKept(root: HTMLElement, threadId: string, rows: Row[], boxFree: boolean): ResumedBox | null {
	const chosen = boxFree ? keptToOpen(root, threadId, rows) : undefined;
	const text = chosen?.body.textContent ?? '';
	const holds = !chosen || (chosen.draft.place ? chatPlaceHolds(text, chosen.draft.place) : offsetsHold(text, chosen.at, chosen.draft.send?.chat?.quote));
	// Its passage gone, it is refused here, before anything is sent: it goes nowhere on its own.
	if (chosen && !holds) {
		keptCommentDrafts.unsend(chosen.key);
		chatCommentDrafts.refused(chosen.key, commentFailure('comment_quote_gone', null, 'comment', 'sent', threadId).text, true);
	}
	for (const key of keptCommentDrafts.pressed()) if (keptCommentDrafts.get(key)?.send?.chat?.threadId === threadId) unsentComments.open(key);
	if (!chosen) return null;
	const { key, at, draft, body } = chosen;
	let rect = body.getBoundingClientRect();
	if (holds) {
		const range = document.createRange();
		range.setStart(...chatTextPoint(body, at.from_off)); range.setEnd(...chatTextPoint(body, at.to_off));
		rect = rangeRect(range) ?? rect;
	}
	const anchor = holds ? chatAnchorAt(text, at.from_off, at.to_off)
		: { from_off: at.from_off, to_off: at.to_off, quote: draft.place?.quote ?? draft.send?.chat?.quote ?? '', anchor_prefix: '', anchor_suffix: '' };
	chatCommentDrafts.closedOn(key);
	return { messageId: at.messageId, seq: chosen.seq, anchor, rect, ...(holds ? {} : { unplaced: true as const }) };
}

/** One transcript's `resumeKept`: it runs once for each chat it shows, the first time that chat is drawn with its messages. */
export class KeptResume {
	#resumedFor: string | null = null;
	/** The box to open, or null; only the first call for a chat with messages brings its kept comments back. */
	run(root: HTMLElement, threadId: string | null, rows: Row[], boxFree: boolean): ResumedBox | null {
		if (!threadId || this.#resumedFor === threadId || !rows.length) return null;
		this.#resumedFor = threadId;
		return resumeKept(root, threadId, rows, boxFree);
	}
}
