/**
 * The one user-global attention queue.
 *
 * The server owns membership, order, and the typed detail each renderer needs.
 * This client holds no cache, derives no truth, and never removes an item from a
 * local array: after any settlement it re-reads.
 *
 * A row leaves the generic attention lists only because the server also supplied
 * the payload that draws it — never because its type asserted someone would.
 */

export type AttentionRenderer = 'decision' | 'external-review';

export interface DecisionOption {
	id: string;
	label: string;
	detail: string;
}

export interface AttentionSource {
	thread_id: string | null;
	thread_label: string;
	project_id: string | null;
	project_label: string | null;
	/** False when the source had no parent tier, so no one reviewed the raise. */
	parent_reviewed: boolean;
}

export interface DecisionDetail {
	id: string;
	plan_id: string;
	node_id: string;
	source_node_id: string;
	thread_id: string;
	title: string;
	context: string;
	tradeoff: string;
	/**
	 * Pick one (the default; a body without `pick` is this): choose exactly one
	 * option. Pick any: independent items ticked in any combination, even none.
	 */
	pick?: 'one' | 'any';
	/** Pick one: the recommended option id. Pick any: the ids to pre-tick, possibly none. */
	recommendation: string | string[];
	rationale: string;
	options: DecisionOption[];
	other_enabled: boolean;
	order_key: number;
	revision: number;
	raised_at: string | null;
	source: AttentionSource;
}

export interface ExternalReviewDetail {
	review_key: string;
	title: string;
	/**
	 * Who asked for the review (PRD-PLANS H4) — the principal the *server*
	 * resolved, never text the caller supplied. `kind` is what tells the reader
	 * whether a person or a lane is speaking; `label` is display only.
	 */
	requester: {
		kind: 'user' | 'thread' | 'service';
		label: string | null;
		lane: string;
	} | null;
	pending_count: number;
	total_count: number;
	node_id: string;
	/** We own the indicator; the raising lane owns the resolution surface. */
	destination: { version?: number; pane: string; id: string } | null;
	source: AttentionSource | null;
}

/** A dock hand-off. Comment replies also name the file needed by the artifact tab. */
export interface AttentionDestination {
	version?: number;
	pane: string;
	id: string;
	path?: string;
	title?: string;
}

export interface CommentReplyDetail {
	id: string;
	title: string;
	unread_count: number;
	newest_at: string;
	destination: AttentionDestination;
	source: { thread_id: string; thread_label: string };
}

export interface CommentReplyItem {
	external_id: string;
	renderer: 'comment-reply';
	detail: CommentReplyDetail;
}

export interface AttentionQueueItem {
	external_id: string;
	renderer: AttentionRenderer;
	detail: DecisionDetail | ExternalReviewDetail;
}

export interface AttentionQueue {
	/** At most one — the queue is worked one at a time (PRD-PLANS E8). */
	current: AttentionQueueItem | null;
	/** How many still wait, including `current`. */
	count: number;
	/** The same order, capped; `items[0]` is `current`. */
	items: AttentionQueueItem[];
	/** Passive replies are separate from the capped Needs-you queue. */
	reply_items?: CommentReplyItem[];
}

/** The backend supplies the item list; it is not reconstructed client-side. */
interface AttentionQueueResponse {
	current?: AttentionQueueItem | null;
	count?: number;
	items: AttentionQueueItem[];
	reply_items?: CommentReplyItem[];
}

/** The reserved answer id for free-form text (PRD-PLANS E4: never strand). */
export const OTHER_OPTION_ID = 'other';

export function isPickAny(decision: Pick<DecisionDetail, 'pick'>): boolean {
	return decision.pick === 'any';
}

/** The ids a pick-any decision pre-ticks, in option order; pick one yields its one id. */
export function recommendedIds(decision: DecisionDetail): string[] {
	const named = Array.isArray(decision.recommendation)
		? decision.recommendation
		: [decision.recommendation];
	return decision.options.map((option) => option.id).filter((id) => named.includes(id));
}

export type AnswerOutcome =
	| { status: 'ok' }
	/** The decision moved under the user; re-read and re-present it. */
	| { status: 'stale' }
	/** The answer itself was refused; the user can fix it without re-reading. */
	| { status: 'rejected'; message: string }
	/** The question was withdrawn or already answered; it will not come back. */
	| { status: 'gone'; message: string }
	| { status: 'unauthorized' }
	| { status: 'error' };

export async function fetchAttentionQueue(bearer: string): Promise<AttentionQueue> {
	const res = await fetch('/api/attention/queue', {
		headers: { Authorization: `Bearer ${bearer}` },
	});
	if (!res.ok) throw new Error(`HTTP ${res.status}`);
	const data = (await res.json()) as AttentionQueueResponse;
	const current = data.current ?? null;
	return {
		current,
		count: data.count ?? 0,
		items: data.items,
		reply_items: data.reply_items,
	};
}

/** A safe, fixed fact for a refusal we cannot attribute — never a raw body. */
const FALLBACK_REJECTION = 'That answer was not accepted.';

async function rejection(res: Response, fallback = FALLBACK_REJECTION): Promise<string> {
	try {
		const body = (await res.json()) as { detail?: unknown };
		return typeof body.detail === 'string' && body.detail.trim() ? body.detail : fallback;
	} catch {
		return fallback;
	}
}

const FALLBACK_CLOSED = 'That decision is closed.';

function closedMessage(res: Response): Promise<string> {
	return rejection(res, FALLBACK_CLOSED);
}

/**
 * Answers one decision.
 *
 * `otherText` is sent ONLY for the reserved Other option. Sending a leftover
 * draft alongside a listed option is refused by the server, and reporting that
 * refusal as staleness made the retry the UI suggested fail identically forever.
 *
 * A pick-any answer carries `optionIds` (possibly none) instead of `optionId`,
 * and its note whenever it is non-blank: there the note is additive.
 */
export async function answerDecision(
	bearer: string,
	decisionId: string,
	answer: {
		optionId?: string;
		optionIds?: string[];
		otherText: string;
		expectedRevision: number;
	},
	mutationId: string,
): Promise<AnswerOutcome> {
	let res: Response;
	try {
		res = await fetch(`/api/plans/decisions/${encodeURIComponent(decisionId)}/answer`, {
			method: 'POST',
			headers: {
				Authorization: `Bearer ${bearer}`,
				'Content-Type': 'application/json',
			},
			body: JSON.stringify(
				answer.optionIds
					? {
							option_ids: answer.optionIds,
							other_text: answer.otherText.trim() ? answer.otherText : null,
							expected_revision: answer.expectedRevision,
							mutation_id: mutationId,
						}
					: {
							option_id: answer.optionId,
							other_text: answer.optionId === OTHER_OPTION_ID ? answer.otherText : null,
							expected_revision: answer.expectedRevision,
							mutation_id: mutationId,
						},
			),
		});
	} catch {
		return { status: 'error' };
	}
	// 403 and 404 are deliberately indistinguishable: the server refuses foreign,
	// missing, and archived targets identically.
	if (res.status === 403 || res.status === 404) return { status: 'unauthorized' };
	// 409 now means one thing only: the decision moved. Everything the user can
	// fix in place arrives as a 422 carrying a safe, specific message.
	if (res.status === 409) return { status: 'stale' };
	// 410 is a question that already left the queue: the server's sentence is
	// written for the user, and re-reading cannot bring the question back.
	if (res.status === 410) return { status: 'gone', message: await closedMessage(res) };
	if (res.status === 422) return { status: 'rejected', message: await rejection(res) };
	if (!res.ok) return { status: 'error' };
	return { status: 'ok' };
}
