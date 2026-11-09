import type { ChatComment as StoredComment } from '$lib/chatCommentTypes';
import { toasts } from '$lib/stores/toast';

export interface ChatComment extends ChatCommentAnchor {
	id: string;
	messageId: string;
	seq: number;
	note: string;
	root_user_message_id?: string;
	root_cid?: string;
}

export interface ChatCommentAnchor {
	from_off: number;
	to_off: number;
	quote: string;
	anchor_prefix: string;
	anchor_suffix: string;
}

/**
 * Send a reply to a stored comment: that comment's own anchor plus the new note, in the shape the composer's
 * send takes. When the comment's message is gone or carries no place in history to answer into, say so and
 * reject instead, so every thread the reply was typed in keeps it.
 */
export function sendCommentReply(message: { id: string; seq?: number | null } | undefined, comment: StoredComment | undefined, note: string, send: (reply: ChatComment) => void): void {
	if (!message || !comment || typeof message.seq !== 'number') {
		toasts.show('CORSIA couldn’t send this reply because its comment isn’t in this chat. Your reply stays here; comment on the text again.', 'error');
		throw new Error('Comment is unavailable');
	}
	send({
		id: crypto.randomUUID(), messageId: message.id, seq: message.seq,
		from_off: comment.from_off, to_off: comment.to_off, quote: comment.quote,
		anchor_prefix: comment.anchor_prefix, anchor_suffix: comment.anchor_suffix, note,
		root_user_message_id: comment.user_message_id, root_cid: comment.cid,
	});
}

/** Clip a selection to the first message body and capture its rendered-text coordinates. */
export function clipChatSelection(selection: Selection, body: HTMLElement): { range: Range; anchor: ChatCommentAnchor } | null {
	if (selection.isCollapsed || selection.rangeCount === 0) return null;
	const original = selection.getRangeAt(0);
	if (!body.contains(original.startContainer)) return null;
	const range = original.cloneRange();
	if (!body.contains(range.endContainer)) {
		range.selectNodeContents(body);
		range.setStart(original.startContainer, original.startOffset);
	}
	const raw = range.toString();
	const quote = raw.trim();
	if (!quote) return null;
	const before = document.createRange();
	before.selectNodeContents(body);
	before.setEnd(range.startContainer, range.startOffset);
	const from_off = before.toString().length + raw.length - raw.trimStart().length;
	const to_off = from_off + quote.length;
	const text = body.textContent ?? '';
	return { range, anchor: { from_off, to_off, quote, anchor_prefix: text.slice(0, from_off), anchor_suffix: text.slice(to_off) } };
}
