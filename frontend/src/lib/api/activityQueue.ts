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

/** A read the server refused or failed, with its status so the view can say which. */
export class ActivityQueueError extends Error {
	constructor(readonly status: number) {
		super(`HTTP ${status}`);
	}
}

export async function fetchActivityQueue(projectId: string | null): Promise<ActivityQueue> {
	const query = new URLSearchParams({ view: 'activity' });
	if (projectId) query.set('project_id', projectId);
	const res = await apiFetch(`/api/attention/queue?${query.toString()}`);
	if (!res.ok) throw new ActivityQueueError(res.status);
	const data = (await res.json()) as Partial<ActivityQueue> & { items?: ActivityQueueItem[] };
	const items = data.items ?? [];
	return { count: data.count ?? items.length, items, ranker: data.ranker ?? null };
}
