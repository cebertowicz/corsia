import { apiFetch } from './request';
import type { AttentionRenderer, AttentionSource, DecisionDetail, HeldReplyDetail } from './attentionQueue';

/**
 * Activity's view of the same queue (`view=activity`): every kind waiting on the person, in the order the scope's
 * ranker set. Only Activity reads this view; the dock and sidebar read the default view above, whose rows and
 * renderers are unchanged.
 */
export type ActivityKind = 'review' | 'decision' | 'task' | 'reply' | 'message';

/** What the pane opens: a document (at a comment), a chat reply in its chat, or nothing (the kind's own panel). */
export type ActivityOpen =
	| { artifact_id: string; comment_id?: string }
	| { thread_id: string; message_id: string }
	| null;

export interface ActivityRank {
	position: number;
	/** One line, written for the person, saying why the item sits where it does. */
	reason: string;
	ranked_by: string;
	ranked_at: string;
}

export interface ActivityRanker {
	thread_id: string;
	label: string;
}

/** A document or image waiting for the person's approval; the document's own review bar answers it. */
export interface ActivityReviewDetail {
	artifact_id: string;
	round?: number | null;
}

/** A plan step only the person can do; Done settles it as the person's step Done does. */
export interface ActivityTaskDetail {
	plan_id: string;
	node_id: string;
	revision: number;
	/** The step's instructions, when the step carries them. */
	description?: string | null;
}

/** An agent answered the person's comment in a document. Done marks the thread read up to `reply_id`. */
export interface ActivityDocReplyDetail {
	artifact_id: string;
	root_id: string;
	reply_id: string;
	/** The person's comment and the agent's answer, when the server sends them. */
	comment?: string | null;
	reply?: string | null;
}

/** An agent answered the person's comment in chat; it opens in its chat. */
export interface ActivityChatReplyDetail {
	thread_id: string;
	message_id: string;
	text?: string | null;
}

export interface ActivityQueueItem {
	/** Stable across reads; what the ranker writes. */
	key: string;
	external_id: string;
	renderer: AttentionRenderer | 'review' | 'task' | 'reply';
	kind: ActivityKind;
	title: string;
	raised_at: string | null;
	source: AttentionSource;
	open: ActivityOpen;
	rank: ActivityRank | null;
	detail:
		| DecisionDetail
		| HeldReplyDetail
		| ActivityReviewDetail
		| ActivityTaskDetail
		| ActivityDocReplyDetail
		| ActivityChatReplyDetail;
}

export interface ActivityQueue {
	/** Every item in the scope, all kinds, uncapped. */
	count: number;
	/** Ranked first by position, then unranked in canonical order. */
	items: ActivityQueueItem[];
	/** Who ranks this scope; its chat opens from the list header. */
	ranker: ActivityRanker | null;
}

/**
 * A read the server answered but that can't be shown: a refusal or failure status, or a body that isn't Activity's
 * view. It carries the status so the view can tell it from a request that never reached the server.
 */
export class ActivityQueueError extends Error {
	constructor(readonly status: number) {
		super(`HTTP ${status}`);
	}
}

const isObject = (value: unknown): value is Record<string, unknown> =>
	!!value && typeof value === 'object' && !Array.isArray(value);
const isString = (value: unknown): value is string => typeof value === 'string';

/** The fields every row is read by: the list keys and labels a row by these, the pane picks its kind's panel. */
function isRow(value: unknown): value is ActivityQueueItem {
	return isObject(value)
		&& isString(value.key)
		&& isString(value.kind)
		&& isString(value.title)
		&& isObject(value.source)
		&& isString(value.source.thread_label);
}

function isActivityQueue(value: unknown): value is Partial<ActivityQueue> & { items: ActivityQueueItem[] } {
	return isObject(value) && Array.isArray(value.items) && value.items.every(isRow);
}

export async function fetchActivityQueue(projectId: string | null): Promise<ActivityQueue> {
	const query = new URLSearchParams({ view: 'activity' });
	if (projectId) query.set('project_id', projectId);
	const res = await apiFetch(`/api/attention/queue?${query.toString()}`);
	if (!res.ok) throw new ActivityQueueError(res.status);
	const data: unknown = await res.json().catch(() => null);
	// Anything else is not this view (an older server's queue, or a broken body): it shows as a failed read, never as
	// an empty list.
	if (!isActivityQueue(data)) throw new ActivityQueueError(res.status);
	return { count: data.count ?? data.items.length, items: data.items, ranker: data.ranker ?? null };
}
