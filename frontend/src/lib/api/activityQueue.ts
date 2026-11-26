import { apiFetch } from './request';
import type { AttentionRenderer, AttentionSource, DecisionDetail, HeldReplyDetail } from './attentionQueue';

/**
 * Activity's view of the same queue (`view=activity`): two lists. Your tasks holds every kind waiting on the person,
 * in the order the scope's ranker set; Notifications holds one row per document with unread replies to the person's
 * comments, newest reply first, never ranked. Only Activity reads this view; the plan pane reads the default view,
 * whose rows and renderers are unchanged.
 */
export type ActivityKind = 'review' | 'decision' | 'task' | 'document' | 'message' | 'draft';

/** What the pane opens: a document (at a comment), or nothing (the kind's own panel). */
export type ActivityOpen = { artifact_id: string; comment_id?: string } | { path: string; artifact_id: string | null } | null;

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
	/** Unread replies to the person's comments on the document under review; absent when there are none. */
	unread_replies?: number;
}

/** A plan step only the person can do; Done settles it as the person's step Done does. */
export interface ActivityTaskDetail {
	plan_id: string;
	node_id: string;
	revision: number;
	/** The step's instructions, when the step carries them. */
	description?: string | null;
}

/**
 * A document where agents answered the person's comments. `unread` is the document's unread set, the one its pane
 * and jump button count; Done marks it read up to `latest_reply_id`, so a reply that lands after stays unread.
 */
export interface ActivityDocumentDetail {
	artifact_id: string;
	unread: number;
	latest_reply_id: string;
	/** The agents who replied, newest first. */
	agents: string[];
	threads: { root_id: string; reply_id: string }[];
}

export interface ActivityQueueItem {
	/** Stable across reads; what the ranker writes. */
	key: string;
	external_id: string;
	renderer: AttentionRenderer | 'review' | 'task' | 'document' | 'draft';
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
		| ActivityDocumentDetail
		| { artifact_id: string | null; missing?: boolean; unread_replies?: number };
}

export interface ActivityQueue {
	/** Every task in the scope, uncapped. */
	count: number;
	/** Ranked first by position, then unranked in canonical order. */
	items: ActivityQueueItem[];
	/** Who ranks this scope; its chat opens from the list header. */
	ranker: ActivityRanker | null;
	/** Documents with unread replies to the person's comments, newest reply first. */
	notifications: { count: number; items: ActivityQueueItem[] };
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

function isList(value: unknown): value is { count?: number; items: ActivityQueueItem[] } {
	return isObject(value) && Array.isArray(value.items) && value.items.every(isRow);
}

type QueueBody = { count?: number; items: ActivityQueueItem[]; ranker?: ActivityRanker | null; notifications?: { count?: number; items: ActivityQueueItem[] } };

function isActivityQueue(value: unknown): value is QueueBody {
	if (!isList(value)) return false;
	const notifications = (value as Record<string, unknown>).notifications;
	return notifications === undefined || isList(notifications);
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
	const notes: { count?: number; items: ActivityQueueItem[] } = data.notifications ?? { items: [] };
	return {
		count: data.count ?? data.items.length,
		items: data.items,
		ranker: data.ranker ?? null,
		notifications: { count: notes.count ?? notes.items.length, items: notes.items },
	};
}

/**
 * Marks documents read up to the reply the person was shown: Done, a dismissed row, and Mark all read all use this one
 * call. It throws on any failure, and the caller shows its own line.
 */
export async function markDocumentsRead(documents: { artifact_id: string; through_reply_id: string }[]): Promise<void> {
	const res = await apiFetch('/api/attention/notifications/read', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ documents }),
	});
	if (!res.ok) throw new ActivityQueueError(res.status);
}
