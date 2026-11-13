import type { ChatComment as StoredComment } from '$lib/chatCommentTypes';

export interface ChatComment extends ChatCommentAnchor {
	id: string;
	messageId: string;
	seq: number;
	note: string;
	root_user_message_id?: string;
	root_cid?: string;
}

/**
 * A comment or reply send that was refused; its message is the sentence the person reads in the box.
 * `final` when no press of the box can send it (its chat is deleted), so the box offers no Retry.
 */
export class CommentSendRefused extends Error {
	constructor(message: string, readonly final = false) { super(message); }
}

/** The sentence for a send that failed for a cause nobody named. */
export const unknownSendFailure = (noun: 'comment' | 'reply') => `Couldn’t send this ${noun}. Try again.`;

/** The sentence a comment's or reply's box shows for a send that threw: the refusal's own cause, or the generic one when the cause is unknown. */
export function refusalText(error: unknown, noun: 'comment' | 'reply'): string {
	return error instanceof CommentSendRefused ? error.message : unknownSendFailure(noun);
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
export function sendCommentReply(message: { id: string; seq?: number | null } | undefined, comment: StoredComment | undefined, note: string, send: (reply: ChatComment) => Promise<void>): Promise<void> {
	if (!message || !comment || typeof message.seq !== 'number') {
		throw new CommentSendRefused('Couldn’t send this reply because its comment isn’t in this chat. Your reply stays here; comment on the text again.');
	}
	return send({
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
