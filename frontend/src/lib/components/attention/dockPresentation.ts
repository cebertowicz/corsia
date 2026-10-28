import type { AttentionQueueItem, DecisionDetail, ExternalReviewDetail } from '$lib/api/attentionQueue';

export function label(item: AttentionQueueItem): string {
	return item.renderer === 'decision'
		? (item.detail as DecisionDetail).title
		: (item.detail as ExternalReviewDetail).title;
}

/** The landmark's name carries the bar's state for assistive technology. */
export function regionLabel(current: AttentionQueueItem | null, count: number): string {
	if (!current) return 'Needs you';
	return count > 1 ? `Needs you — ${label(current)}, ${count} waiting` : `Needs you — ${label(current)}`;
}

export function decisionMeta(decision: DecisionDetail): string {
	return `Decision · ${decision.source.thread_label}${decision.source.project_label ? ` · ${decision.source.project_label}` : ''}`;
}
