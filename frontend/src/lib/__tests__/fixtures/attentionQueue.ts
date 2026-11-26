/** Shared fixtures matching the production `/api/attention/queue` contract. */
import { vi } from 'vitest';

import type { ActivityDocumentDetail, ActivityQueue, ActivityQueueItem, ActivityRank } from '$lib/api/activityQueue';
import type {
	AttentionQueue,
	AttentionQueueItem,
	AttentionSource,
	DecisionDetail,
	ExternalReviewDetail,
	HeldReplyDetail,
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

export function heldReplyItem(over: Partial<HeldReplyDetail> = {}): AttentionQueueItem {
	const detail: HeldReplyDetail = {
		id: 'held-1',
		thread_id: 'child',
		title: "Send Sender Thread's reply to the Researcher agent on Anthropic?",
		text: 'First line of the reply.\n\n  Indented second paragraph.',
		recipient: 'the Researcher agent',
		provider: 'Anthropic',
		error: null,
		raised_at: '2026-08-02T10:00:00+00:00',
		source: { ...SOURCE, thread_label: 'Sender Thread' },
		...over,
	};
	return { external_id: detail.id, renderer: 'held-reply', detail };
}

/* Activity's view (`view=activity`): every kind, ranked, as the Contract returns it. */

const HOUR = 3_600_000;
/** A fixed clock for Activity fixtures, so ages read the same in tests and frames. */
export const ACTIVITY_NOW = Date.parse('2026-11-24T05:00:00Z');
const ago = (hours: number) => new Date(ACTIVITY_NOW - hours * HOUR).toISOString();

export function activitySource(label: string, threadId: string): AttentionSource {
	return { thread_id: threadId, thread_label: label, project_id: 'project-1', project_label: 'CORSIA', parent_reviewed: true };
}

function rank(position: number, reason: string): ActivityRank {
	return { position, reason, ranked_by: 'thread-conductor', ranked_at: '2026-11-24T04:10:00Z' };
}

const RENDERER = { review: 'review', decision: 'decision', task: 'task', document: 'document', message: 'held-reply', chat: 'chat' } as const;

export function activityItem(
	over: Partial<ActivityQueueItem> & Pick<ActivityQueueItem, 'key' | 'kind'>,
): ActivityQueueItem {
	return {
		external_id: over.key,
		renderer: RENDERER[over.kind],
		title: 'An item waiting on you',
		raised_at: ago(1),
		source: activitySource('Governance & Scopes', 'thread-governance'),
		open: null,
		rank: null,
		detail: { artifact_id: 'art-default' },
		...over,
	};
}

/** The seven to-dos the approved frames draw, in their order: every to-do kind at least once, and a review with replies. */
export function activityFrameItems(): ActivityQueueItem[] {
	const conductor = activitySource('CORSIA Conductor', 'thread-conductor');
	const ecosystem = activitySource('Ecosystem', 'thread-ecosystem');
	return [
		activityItem({
			key: 'review:credentials', kind: 'review',
			title: 'Approve or change how agents sign in to sites with your saved passwords.',
			raised_at: ago(24 * 5), open: { artifact_id: 'art-credentials' },
			rank: rank(1, "0.5.0 publishes Nov 24, and agents can't sign in to a site without you there."),
			detail: { artifact_id: 'art-credentials', round: 1 },
		}),
		activityItem({
			key: 'decision:docs-review', kind: 'decision', external_id: 'decision-docs',
			title: 'Ship 0.5.0 without each paused agent reviewing its public docs, or wake them to do it?',
			raised_at: ago(1), source: conductor,
			rank: rank(2, 'It decides whether 0.5.0 can publish on your date.'),
			detail: decisionDetail({
				id: 'decision-docs', thread_id: 'thread-conductor', source: conductor,
				title: 'Ship 0.5.0 without each paused agent reviewing its public docs, or wake them to do it?',
				context: 'The release steps say each agent reviews its public docs before a release. Seven of the owners are paused.',
				recommendation: 'ship', other_enabled: false,
				options: [
					{ id: 'ship', label: 'Ship without their review', detail: 'The conductor checks the docs against the build itself.' },
					{ id: 'wake', label: 'Wake the seven to review', detail: 'Each reviews its own docs tonight.' },
				],
			}),
		}),
		activityItem({
			key: 'review:instructions', kind: 'review',
			title: 'Read the final workspace, project and agent instruction files before they ship.',
			raised_at: ago(2), source: activitySource('Agent Substrate', 'thread-substrate'),
			open: { artifact_id: 'art-instructions' }, rank: rank(3, 'Every agent in 0.5.0 loads these.'),
			detail: { artifact_id: 'art-instructions' },
		}),
		activityItem({
			key: 'review:readme', kind: 'review',
			title: 'Approve the README: download first, the licence, and what it costs to run.',
			raised_at: ago(3), source: ecosystem,
			open: { artifact_id: 'art-readme' }, rank: rank(4, "It's the first page a stranger reads."),
			detail: { artifact_id: 'art-readme' },
		}),
		activityItem({
			key: 'task:sign-build', kind: 'task',
			title: 'Sign the 0.5.0 build: enter the code Apple sends you when asked.',
			raised_at: ago(3), source: activitySource('Packaging & Onboarding', 'thread-packaging'),
			rank: rank(5, "An unsigned Mac app won't open for a stranger."),
			detail: {
				plan_id: 'plan-packaging', node_id: 'node-sign', revision: 4,
				description: '1. Packaging & Onboarding starts the signing.\n2. Sign in with your Apple ID and enter the code Apple sends.\n3. Press Done.',
			},
		}),
		activityItem({
			key: 'message:intel-mac', kind: 'message', external_id: 'held-intel',
			title: "Send Ecosystem's reply on the public issue asking whether CORSIA runs on an Intel Mac?",
			raised_at: ago(4), source: ecosystem,
			rank: rank(6, 'It goes to someone outside, so it waits for your yes.'),
			detail: heldReplyItem({
				id: 'held-intel', thread_id: 'thread-ecosystem', source: ecosystem,
				title: "Send Ecosystem's reply on the public issue asking whether CORSIA runs on an Intel Mac?",
				text: "Thanks for asking. We haven't tried 0.5.0 on an Intel Mac yet; we'll post here once we have.",
				recipient: 'the public issue', provider: 'GitHub',
			}).detail as HeldReplyDetail,
		}),
		activityItem({
			key: 'review:later-row', kind: 'review',
			title: "Approve the plan pane's Later row.",
			raised_at: ago(26), source: activitySource('Structured Plans', 'thread-plans'),
			open: { artifact_id: 'art-later' },
			rank: rank(7, 'Nothing else waits on it; it carries replies to your comments.'),
			detail: { artifact_id: 'art-later', unread_replies: 3 },
		}),
	];
}

/** Forty items, the fourth with a very long title. */
export function activityFortyItems(): ActivityQueueItem[] {
	const long = activityItem({
		key: 'review:when-to-ask', kind: 'review',
		title: "Approve or change the wording every agent loads on each turn about when to ask you, when to decide and tell you afterwards, and when to leave it to the project's conductor, including the three cases you corrected last week and the new line on dates.",
		raised_at: ago(6), source: activitySource('Agent Substrate', 'thread-substrate'),
		open: { artifact_id: 'art-when-to-ask' }, rank: rank(4, 'Every agent loads this text.'),
		detail: { artifact_id: 'art-when-to-ask' },
	});
	const rest = Array.from({ length: 36 }, (_, k) => activityItem({
		key: `review:bulk-${k}`, kind: 'review', title: `Approve change ${k + 1} before it ships.`,
		raised_at: ago(24 * (k + 1) + 1), source: activitySource('Quality Gate', 'thread-quality'),
		open: { artifact_id: `art-bulk-${k}` }, rank: rank(k + 5, 'Ranked after the release items.'),
		detail: { artifact_id: `art-bulk-${k}` },
	}));
	return [...activityFrameItems().slice(0, 3), long, ...rest];
}

/** A document with unread replies to the person's comments, as Notifications lists it. */
export function activityDocument(
	key: string, over: Partial<ActivityDocumentDetail> = {}, raisedHoursAgo = 2,
): ActivityQueueItem {
	const artifactId = over.artifact_id ?? `art-${key}`;
	return activityItem({
		key: `doc:${artifactId}`, kind: 'document', external_id: `doc:${artifactId}`,
		title: `${key}.md`, raised_at: ago(raisedHoursAgo), source: activitySource('Structured Plans', 'thread-plans'),
		open: { artifact_id: artifactId, comment_id: over.threads?.[0]?.root_id ?? `${key}-root` },
		detail: {
			artifact_id: artifactId, unread: 2, latest_reply_id: `${key}-reply-2`, agents: ['Structured Plans'],
			threads: [{ root_id: `${key}-root`, reply_id: `${key}-reply-2` }], ...over,
		},
	});
}

export function activityQueueBody(
	items: ActivityQueueItem[] = activityFrameItems(), notifications: ActivityQueueItem[] = [],
): ActivityQueue {
	return {
		count: items.length, items, ranker: { thread_id: 'thread-conductor', label: 'CORSIA Conductor' },
		notifications: { count: notifications.length, items: notifications },
	};
}
