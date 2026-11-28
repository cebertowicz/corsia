import type {
	ActivityDocumentDetail,
	ActivityKind,
	ActivityQueueItem,
	ActivityReviewDetail,
	ActivityTaskDetail,
} from '$lib/api/activityQueue';
import { isPickAny, OTHER_OPTION_ID, type DecisionDetail, type HeldReplyDetail } from '$lib/api/attentionQueue';
export { findDocument } from '$lib/findDocument';
import { parseWsTimestamp } from '$lib/time';

export const KIND_LABEL: Record<ActivityKind, string> = {
	review: 'Review',
	decision: 'Decision',
	task: 'Task',
	document: 'Document',
	message: 'Message',
	draft: 'Draft',
};

/** Activity's two lists. A document with unread replies is a notification; everything else is a task. */
export type ActivityTab = 'tasks' | 'notifications';
export const EMPTY_LINE: Record<ActivityTab, string> = { tasks: 'Nothing needs you.', notifications: 'No unread replies.' };
export type DecisionDraft = { selected: string | null; picked: string[]; other: string };
export const tabOf = (item: ActivityQueueItem): ActivityTab => (item.kind === 'document' ? 'notifications' : 'tasks');

/** "1 unread reply", "5 unread replies". */
export const unreadRepliesLabel = (count: number) => `${count} unread repl${count === 1 ? 'y' : 'ies'}`;

/** Who replied, for the opened document's bar: "Governance & Scopes", "A and B", "A and 2 others". */
export function agentsLabel(agents: readonly string[]): string {
	if (agents.length <= 1) return agents[0] ?? 'An agent';
	if (agents.length === 2) return `${agents[0]} and ${agents[1]}`;
	return `${agents[0]} and ${agents.length - 1} others`;
}

/**
 * Where the person is in a document's replies. `total` is fixed when the document opens and rises only when a reply
 * arrives; `unread` is what the jump button still counts. So the position plus the jump button's count is the total.
 */
export function replyPosition(total: number, unread: number): { at: number; of: number } {
	const of = Math.max(total, unread, 1);
	return { at: Math.min(of, Math.max(1, of - unread)), of };
}

/** How long an item has waited, in the list's words: "1 hour", "yesterday", "5 days". */
export function ageLabel(iso: string | null, now = Date.now()): string {
	const at = parseWsTimestamp(iso);
	if (!Number.isFinite(at)) return '';
	const minutes = Math.max(0, Math.floor((now - at) / 60_000));
	if (minutes < 1) return 'just now';
	if (minutes < 60) return minutes === 1 ? '1 minute' : `${minutes} minutes`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return hours === 1 ? '1 hour' : `${hours} hours`;
	const days = Math.floor(hours / 24);
	if (days < 2) return 'yesterday';
	return `${days} days`;
}

/** When a document's latest reply came, in its row's words: "latest 1 hour ago", "latest yesterday". */
export function latestLabel(iso: string | null, now = Date.now()): string {
	const age = ageLabel(iso, now);
	if (!age) return '';
	return `latest ${age === 'yesterday' || age === 'just now' ? age : `${age} ago`}`;
}

/** When the ranker last set the order: the newest rank among the items, or null when none is ranked. */
export function rankedAt(items: readonly ActivityQueueItem[]): string | null {
	let newest: string | null = null;
	let newestAt = Number.NEGATIVE_INFINITY;
	for (const item of items) {
		const at = parseWsTimestamp(item.rank?.ranked_at ?? null);
		if (Number.isFinite(at) && at > newestAt) {
			newestAt = at;
			newest = item.rank?.ranked_at ?? null;
		}
	}
	return newest;
}

/**
 * Which item opens once the open one has left the list: the first item that followed it in the order the person
 * last saw that is still listed, else whatever now sits at its place, else the last item (the row above the last
 * one acted on), else nothing.
 */
export function followerOf(
	previous: readonly string[],
	gone: string,
	items: readonly ActivityQueueItem[],
): string | null {
	const listed = new Set(items.map((item) => item.key));
	const at = previous.indexOf(gone);
	if (at >= 0) {
		for (const key of previous.slice(at + 1)) if (listed.has(key)) return key;
	}
	if (items.length === 0) return null;
	const place = Math.max(0, Math.min(at, items.length - 1));
	return items[place]?.key ?? null;
}

/**
 * The list with the item the person is still reading after a refusal or departure, at the place it held in the order
 * they last saw. Once they move on it is no longer kept, and the list is the server's alone.
 */
export function keepInPlace(
	items: readonly ActivityQueueItem[],
	kept: ActivityQueueItem | null,
	previous: readonly string[],
): readonly ActivityQueueItem[] {
	if (!kept || items.some((item) => item.key === kept.key)) return items;
	const at = previous.indexOf(kept.key);
	const place = at < 0 ? items.length : Math.min(at, items.length);
	return [...items.slice(0, place), kept, ...items.slice(place)];
}

export function documentOf(item: ActivityQueueItem): { artifactId: string | null; path?: string; commentId?: string } | null {
	if (!item.open) return null;
	if ('path' in item.open) return { artifactId: item.open.artifact_id, path: item.open.path };
	return { artifactId: item.open.artifact_id, commentId: item.open.comment_id };
}


/** Find a rekeyed row for the same open document and reply. */
export function matchingDocumentRows(lastOpen: ActivityQueueItem, current: readonly ActivityQueueItem[]): ActivityQueueItem[] {
	const doc = documentOf(lastOpen);
	return doc ? current.filter((item) => {
		const candidate = documentOf(item);
		return item.kind === lastOpen.kind && !!candidate
			&& (doc.artifactId ? candidate.artifactId === doc.artifactId : !!doc.path && candidate.path === doc.path)
			&& candidate.commentId === doc.commentId && item.source.thread_id === lastOpen.source.thread_id;
	}) : [];
}

/** The document that can stay open after its review or email draft settles. */
export function settledArtifact(item: ActivityQueueItem | null): string | null | undefined {
	return item?.kind === 'review' ? documentOf(item)?.artifactId
		: item?.kind === 'draft' && 'unread_replies' in item.detail && item.detail.unread_replies
			&& 'artifact_id' in item.detail ? item.detail.artifact_id : undefined;
}

/** Read through the latest reply shown by a document row. */
export function readThrough(item: ActivityQueueItem) {
	const detail = documentDetailOf(item);
	return detail ? { artifact_id: detail.artifact_id, through_reply_id: detail.latest_reply_id } : null;
}

/** A pick-any answer may be empty; a free-text option needs text. */
export function answerIsReady(decision: DecisionDetail | null, selected: string | null, other: string): boolean {
	if (!decision) return false;
	if (isPickAny(decision)) return true;
	if (!selected) return false;
	return selected !== OTHER_OPTION_ID || other.trim().length > 0;
}

export const decisionOf = (item: ActivityQueueItem) =>
	item.kind === 'decision' ? (item.detail as DecisionDetail) : null;
export const heldOf = (item: ActivityQueueItem) =>
	item.kind === 'message' ? (item.detail as HeldReplyDetail) : null;
export const taskOf = (item: ActivityQueueItem) =>
	item.kind === 'task' ? (item.detail as ActivityTaskDetail) : null;
export const reviewOf = (item: ActivityQueueItem) =>
	item.kind === 'review' || item.kind === 'draft' ? (item.detail as ActivityReviewDetail) : null;
export function documentDetailOf(item: ActivityQueueItem): ActivityDocumentDetail | null {
	if (item.kind !== 'document') return null;
	const detail = item.detail as Partial<ActivityDocumentDetail>;
	return detail.artifact_id && detail.latest_reply_id ? (detail as ActivityDocumentDetail) : null;
}
export type TaskSteps =
	| { kind: 'numbered'; start: number; items: string[] }
	| { kind: 'bulleted'; items: string[] }
	| { kind: 'text'; text: string };

/** A task's text as a list when every line is a numbered step, or every line a bullet; otherwise the text as written. */
export function taskSteps(text: string): TaskSteps {
	const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
	const numbered = lines.map((line) => /^(\d+)[.)]\s+(.+)$/.exec(line));
	if (lines.length && numbered.every(Boolean)) {
		return { kind: 'numbered', start: Number(numbered[0]![1]), items: numbered.map((match) => match![2]) };
	}
	const bulleted = lines.map((line) => /^[-*•]\s+(.+)$/.exec(line));
	if (lines.length && bulleted.every(Boolean)) return { kind: 'bulleted', items: bulleted.map((match) => match![1]) };
	return { kind: 'text', text };
}


export type { DocumentLookup } from '$lib/findDocument';
