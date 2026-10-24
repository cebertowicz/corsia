import type { AttentionQueue, AttentionQueueItem, DecisionDetail, ExternalReviewDetail } from '$lib/api/attentionQueue';

// Unread document replies occupy their own persistent chip immediately above
// the dock. Needs-you retains its queue and one-at-a-time action semantics.
export function dockQueue(queue: AttentionQueue): AttentionQueue {
	const items = queue.items.filter((item) => item.renderer !== 'comment-reply');
	// The server's count can exceed the loaded rows; only the reply rows this dock hides come off it.
	return { current: items[0] ?? null, count: queue.count - (queue.items.length - items.length), items };
}

export function label(item: AttentionQueueItem): string {
	return item.renderer === 'decision'
		? (item.detail as DecisionDetail).title
		: (item.detail as ExternalReviewDetail).title;
}

export function decisionMeta(decision: DecisionDetail): string {
	return `Decision · ${decision.source.thread_label}${decision.source.project_label ? ` · ${decision.source.project_label}` : ''}`;
}
