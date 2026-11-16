/** The server's delivery of the input that carried a comment or a person's reply; a held or ended one shows in its thread as chat shows it. */
export type Delivery = {
	delivery_state?: string | null; delivery_error?: string | null;
	/** With `active_turn_id`, what tells a held queued input from one being sent (messageDelivery.deliveryPresentation). */
	write_outcome?: 'accepted' | 'proved_no_write' | 'ambiguous' | null; active_turn_id?: string | null; delivering_reason?: string | null;
};

export type ChatCommentAnswer = {
	user_message_id: string;
	cid: string;
	text: string;
	created_at: string;
	unread: boolean;
} & (
	| ({ author_kind: 'user'; assistant_message_id?: never } & Delivery)
	| { author_kind: 'agent'; assistant_message_id: string }
);

export interface ChatComment extends Delivery {
	id: string;
	cid: string;
	user_message_id: string;
	quote: string;
	note: string;
	from_off: number;
	to_off: number;
	anchor_prefix: string;
	anchor_suffix: string;
	created_at: string;
	answers: ChatCommentAnswer[];
}

/**
 * An agent answer read here stays read when a page fetched before the read arrives after it. The server's seen mark only
 * moves forward and an answer's time never changes, so a read answer never turns unread again; only a stale page says so.
 */
export function keepReadAnswers(existing: ChatComment[] | undefined, incoming: ChatComment[] | undefined): ChatComment[] | undefined {
	if (!existing?.length || !incoming?.length) return incoming;
	const key = (answer: ChatCommentAnswer) => answer.author_kind === 'agent' ? `${answer.user_message_id}:${answer.cid}:${answer.assistant_message_id}` : null;
	const read = new Set(existing.flatMap(comment => comment.answers.filter(answer => !answer.unread).map(key).filter(Boolean)));
	const staleUnread = (answer: ChatCommentAnswer) => answer.unread && read.has(key(answer));
	return incoming.map(comment => comment.answers.some(staleUnread)
		? { ...comment, answers: comment.answers.map(answer => staleUnread(answer) ? { ...answer, unread: false } : answer) }
		: comment);
}
