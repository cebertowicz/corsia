/** Shared fixtures matching the production `/api/attention/queue` contract. */
import { vi } from 'vitest';

import type {
	AttentionQueue,
	AttentionQueueItem,
	DecisionDetail,
	ExternalReviewDetail,
} from '$lib/api/attentionQueue';

export const SOURCE = {
	thread_id: 'child',
	thread_label: 'Deep subagent',
	project_id: 'project-1',
	project_label: 'Project One',
	parent_reviewed: true,
};

export function decisionDetail(over: Partial<DecisionDetail> = {}): DecisionDetail {
	return {
		id: 'decision-1',
		plan_id: 'plan-1',
		node_id: 'node-decision',
		source_node_id: 'node-source',
		thread_id: 'child',
		title: 'Where should decision state live?',
		context: 'P5 needs one canonical home for decision content.',
		tradeoff: 'A table is typed; the identity avoids a second row.',
		recommendation: 'B',
		rationale: 'CORSIA owns the identity, so no twin is required.',
		options: [
			{ id: 'A', label: 'Ship the twin', detail: 'Adds one table.' },
			{ id: 'B', label: 'Reuse the identity', detail: 'No schema change.' },
		],
		other_enabled: true,
		order_key: 0,
		revision: 3,
		raised_at: '2026-08-02T10:00:00+00:00',
		source: { ...SOURCE },
		...over,
	};
}

export function decisionItem(over: Partial<DecisionDetail> = {}): AttentionQueueItem {
	const detail = decisionDetail(over);
	return { external_id: detail.id, renderer: 'decision', detail };
}

/** A pick-any decision: three independent extras, the first two recommended. */
export function pickAnyDetail(over: Partial<DecisionDetail> = {}): DecisionDetail {
	return decisionDetail({
		id: 'decision-any',
		title: 'Add these to your agents?',
		tradeoff: 'Ticked items take a few minutes now; unticked ones can be added later.',
		rationale: 'None of them changes anything else on this Mac.',
		pick: 'any',
		recommendation: ['A', 'B'],
		options: [
			{ id: 'A', label: 'Bring over your MCP servers (3)', detail: 'Copies your config.' },
			{ id: 'B', label: 'Web search', detail: 'Needs a search key.' },
			{ id: 'C', label: 'Daily summary', detail: 'One note each morning.' },
		],
		...over,
	});
}

export function pickAnyItem(over: Partial<DecisionDetail> = {}): AttentionQueueItem {
	const detail = pickAnyDetail(over);
	return { external_id: detail.id, renderer: 'decision', detail };
}

export function externalReviewDetail(
	over: Partial<ExternalReviewDetail> = {},
): ExternalReviewDetail {
	return {
		review_key: 'rk-1',
		title: 'Reviewer findings on PR 9',
		requester: { kind: 'thread', label: 'Reviewer subagent', lane: 'documents' },
		pending_count: 2,
		total_count: 5,
		node_id: 'node-review',
		destination: { version: 1, pane: 'documents', id: 'doc-9' },
		source: { ...SOURCE },
		...over,
	};
}

export function externalReviewItem(
	over: Partial<ExternalReviewDetail> = {},
): AttentionQueueItem {
	const detail = externalReviewDetail(over);
	return { external_id: `pr-${detail.review_key}`, renderer: 'external-review', detail };
}

export function queueBody(
	current: AttentionQueueItem | null = decisionItem(),
	count = 1,
	items: AttentionQueueItem[] = current ? [current] : [],
): AttentionQueue {
	return { current, count, items };
}

/**
 * Adds the required empty attention queue response to a component test's
 * otherwise endpoint-specific fetch stub.
 */
export function withEmptyAttentionQueue(fetchImpl: typeof fetch): typeof fetch {
	return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		if (String(input).includes('/api/attention/queue')) {
			return {
				ok: true,
				status: 200,
				json: async () => queueBody(null, 0),
			} as Response;
		}
		return fetchImpl(input, init);
	}) as typeof fetch;
}
