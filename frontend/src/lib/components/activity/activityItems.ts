import type {
	ActivityChatReplyDetail,
	ActivityDocReplyDetail,
	ActivityKind,
	ActivityQueueItem,
	ActivityReviewDetail,
	ActivityTaskDetail,
} from '$lib/api/activityQueue';
import type { DecisionDetail, HeldReplyDetail } from '$lib/api/attentionQueue';
import { fetchArtifactPane } from '$lib/api/docs';
import type { ArtifactNode, PaneNode } from '$lib/api/artifactPane';
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

export function documentOf(item: ActivityQueueItem): { artifactId: string; commentId?: string } | null {
	if (item.open && 'artifact_id' in item.open) {
		return { artifactId: item.open.artifact_id, commentId: item.open.comment_id };
	}
	return null;
}

export function chatOf(item: ActivityQueueItem): { threadId: string; messageId: string } | null {
	if (item.open && 'message_id' in item.open) return { threadId: item.open.thread_id, messageId: item.open.message_id };
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
export const chatReplyOf = (item: ActivityQueueItem) =>
	item.kind === 'reply' && chatOf(item) ? (item.detail as ActivityChatReplyDetail) : null;

export type DocumentLookup =
	| { status: 'found'; path: string; title: string }
	| { status: 'missing' }
	| { status: 'failed' };

/**
 * The app opens a document by its path, so an item's artifact id is looked up in the files of the chat it came
 * from, the same lookup an outside wait's document uses.
 */
export async function findDocument(threadId: string, artifactId: string): Promise<DocumentLookup> {
	let nodes: PaneNode[];
	try {
		nodes = (await fetchArtifactPane(threadId)).nodes;
	} catch {
		return { status: 'failed' };
	}
	const found = nodes
		.flatMap((node): ArtifactNode[] => (node.kind === 'artifact' ? [node] : node.children))
		.find((node) => node.id === artifactId);
	const path = found?.path ?? found?.external_id;
	if (!found || !path) return { status: 'missing' };
	return { status: 'found', path, title: found.title ?? found.name };
}
