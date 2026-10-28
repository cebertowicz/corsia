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
