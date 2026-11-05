import type { AnswerState } from '$lib/stores/attentionAnswerState';
import type {
	AttentionQueueItem,
	DecisionDetail,
	ExternalReviewDetail,
	HeldReplyDetail,
} from '$lib/api/attentionQueue';

export function label(item: AttentionQueueItem): string {
	return (item.detail as DecisionDetail | ExternalReviewDetail | HeldReplyDetail).title;
}

/** The landmark's name carries the bar's state for assistive technology. */
export function regionLabel(current: AttentionQueueItem | null, count: number): string {
	if (!current) return 'Needs you';
	return count > 1 ? `Needs you — ${label(current)}, ${count} waiting` : `Needs you — ${label(current)}`;
}

function sourceMeta(source: DecisionDetail['source']): string {
	return `${source.thread_label}${source.project_label ? ` · ${source.project_label}` : ''}`;
}

export function decisionMeta(decision: DecisionDetail): string {
	return `Decision · ${sourceMeta(decision.source)}`;
}

/** A held reply's drawer eyebrow: its place in the queue once more than one item waits. */
export function heldMeta(held: HeldReplyDetail, position: number, count: number): string {
	return `${count > 1 ? `${position} of ${count}` : 'Held reply'} · ${sourceMeta(held.source)}`;
}

/** The bar's action word. */
export function actionWord(item: AttentionQueueItem, failed: boolean): string {
	if (item.renderer === 'held-reply') return 'Review';
	if (failed) return 'Not saved — Retry';
	return item.renderer === 'decision' ? 'Answer' : 'Review';
}

/** The dock's receipt while a write is in flight: its own line, or the generic one. */
export function recordingLine(all: Record<string, AnswerState>): string | null {
	const sending = Object.values(all).filter((answer) => answer.state === 'sending');
	if (sending.length === 0) return null;
	return (sending[0] as { line?: string }).line ?? 'Answer recorded';
}
