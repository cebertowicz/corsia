import type {
	ActivityDocReplyDetail,
	ActivityKind,
	ActivityQueueItem,
	ActivityReviewDetail,
	ActivityTaskDetail,
} from '$lib/api/activityQueue';
import type { DecisionDetail, HeldReplyDetail } from '$lib/api/attentionQueue';
export { findDocument } from '$lib/findDocument';
import { parseWsTimestamp } from '$lib/time';

export const KIND_LABEL: Record<ActivityKind, string> = {
	review: 'Review',
	decision: 'Decision',
	task: 'Task',
	reply: 'Reply',
	message: 'Message',
};

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

/** The item after `key` in the list, without acting on it; null on the last item. */
export function nextKey(items: readonly ActivityQueueItem[], key: string | null): string | null {
	const at = items.findIndex((item) => item.key === key);
	if (at < 0) return items[0]?.key ?? null;
	return items[at + 1]?.key ?? null;
}

/**
 * Which item opens once the open one has left the list: the first item that followed it in the order the person
 * last saw that is still listed, else whatever now sits at its place, else the last item, else nothing.
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
 * The list with the item the person is still reading after the server let it go (its press was refused and it left),
 * at the place it held in the order they last saw. Once they move on it is no longer kept, and the list is the
 * server's alone.
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

export function documentOf(item: ActivityQueueItem): { artifactId: string; commentId?: string } | null {
	if (item.open && 'artifact_id' in item.open) {
		return { artifactId: item.open.artifact_id, commentId: item.open.comment_id };
	}
	return null;
}


export const decisionOf = (item: ActivityQueueItem) =>
	item.kind === 'decision' ? (item.detail as DecisionDetail) : null;
export const heldOf = (item: ActivityQueueItem) =>
	item.kind === 'message' ? (item.detail as HeldReplyDetail) : null;
export const taskOf = (item: ActivityQueueItem) =>
	item.kind === 'task' ? (item.detail as ActivityTaskDetail) : null;
export const reviewOf = (item: ActivityQueueItem) =>
	item.kind === 'review' ? (item.detail as ActivityReviewDetail) : null;
export function docReplyOf(item: ActivityQueueItem): ActivityDocReplyDetail | null {
	if (item.kind !== 'reply') return null;
	const detail = item.detail as Partial<ActivityDocReplyDetail>;
	return detail.artifact_id && detail.root_id && detail.reply_id ? (detail as ActivityDocReplyDetail) : null;
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
