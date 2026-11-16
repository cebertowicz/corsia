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

/** Whether two values the server sent as JSON hold the same data, whatever order their keys came in. */
function sameData(a: unknown, b: unknown): boolean {
	if (a === b) return true;
	if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null || Array.isArray(a) !== Array.isArray(b)) return false;
	const left = a as Record<string, unknown>, right = b as Record<string, unknown>;
	const keys = Object.keys(left);
	return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) && sameData(left[key], right[key]));
}

/**
 * Adopts a page's comments over the ones on screen.
 *
 * An agent answer read here stays read when a page fetched before the read arrives after it. The server's seen mark only
 * moves forward and an answer's time never changes, so a read answer never turns unread again; only a stale page says so.
 *
 * A comment the page states unchanged stays the very object on screen (and so does the list, when none changed): what
 * draws comments by identity (the margin column) is not rebuilt, and a refresh that changed nothing moves nothing.
 */
export function adoptPageComments(existing: ChatComment[] | undefined, incoming: ChatComment[] | undefined): ChatComment[] | undefined {
	if (!existing?.length || !incoming?.length) return incoming;
	const key = (answer: ChatCommentAnswer) => answer.author_kind === 'agent' ? `${answer.user_message_id}:${answer.cid}:${answer.assistant_message_id}` : null;
	const read = new Set(existing.flatMap(comment => comment.answers.filter(answer => !answer.unread).map(key).filter(Boolean)));
	const staleUnread = (answer: ChatCommentAnswer) => answer.unread && read.has(key(answer));
	const shown = new Map(existing.map(comment => [comment.id, comment]));
	const adopted = incoming.map(comment => {
		const next = comment.answers.some(staleUnread)
			? { ...comment, answers: comment.answers.map(answer => staleUnread(answer) ? { ...answer, unread: false } : answer) }
			: comment;
		const prior = shown.get(next.id);
		return prior && sameData(prior, next) ? prior : next;
	});
	return adopted.length === existing.length && adopted.every((comment, index) => comment === existing[index]) ? existing : adopted;
}
