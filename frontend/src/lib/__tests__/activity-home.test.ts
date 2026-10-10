import { describe, expect, it } from 'vitest';
import {
	buildActivityGroups,
	buildResumeThreads,
	nextPromotionExpiry,
	rowTier,
	PROMOTION_QUIET_WINDOW_MS,
} from '$lib/activityHome';
import type { ActivityIndexRow } from '$lib/api/plans';
import type { Thread } from '$lib/stores/threads';
import type { ProjectInfo } from '$lib/stores/projects';

function thread(
	id: string,
	updatedAt: string,
	extras: Partial<Thread> = {},
): Thread {
	return {
		id,
		user_id: 'user-1',
		profile: 'personal',
		title: id,
		model: null,
		created_at: updatedAt,
		updated_at: updatedAt,
		...extras,
	};
}

// Activity grouping consumes only the compact scope-index contract.

function displayFor(
	state: string,
	done: number,
	total: number,
	liveness: 'advancing' | 'idle' | 'blocked' | 'needs-review' | 'waiting' | 'paused',
) {
	if (done === total) return { label_key: 'finished' as const, tone_key: 'complete' as const };
	if (liveness === 'advancing' || liveness === 'needs-review' || liveness === 'idle' && state !== 'paused') {
		return { label_key: 'working' as const, tone_key: 'active' as const };
	}
	return { label_key: 'waiting' as const, tone_key: 'waiting' as const };
}

function plan(
	id: string,
	threadId: string | null,
	projectId: string | null,
	state: string,
	done: number,
	total: number,
	liveness: 'advancing' | 'idle' | 'blocked' | 'needs-review' | 'waiting' | 'paused',
	updatedAt = '2026-08-14T12:00:00Z',
): ActivityIndexRow {
	return {
		plan: {
			id,
			owner_kind: threadId ? 'thread' : projectId ? 'project_scope' : 'workspace_scope',
			owner_id: threadId ?? projectId ?? 'workspace',
			state,
			revision: 1,
			updated_at: updatedAt,
		},
		owner: {
			label: threadId ?? projectId ?? 'Workspace',
			project_id: projectId,
			thread_id: threadId,
		},
		progress: { open: total - done, liveness },
		has_nodes: total > 0,
		display: displayFor(state, done, total, liveness),
		priority_at: updatedAt,
	};
}

/** Apply the canonical compact display selected by the backend for each reviewer kind. */
function withReview(
	entry: ActivityIndexRow,
	kind: 'human_decision' | 'human_review' | 'external' | 'parent_agent',
): ActivityIndexRow {
	const display = kind === 'human_decision' || kind === 'human_review'
		? { label_key: 'needs_you' as const, tone_key: 'human-review' as const }
		: kind === 'external'
			? { label_key: 'waiting' as const, tone_key: 'waiting' as const }
			: { label_key: 'working' as const, tone_key: 'active' as const };
	return { ...entry, display };
}

function withUserAction(entry: ActivityIndexRow): ActivityIndexRow {
	return {
		...entry,
		display: { label_key: 'needs_you', tone_key: 'human-review' },
		priority_at: entry.plan.updated_at,
	};
}

const PROJECTS: ProjectInfo[] = [
	{ id: 'p1', name: 'CORSIA', status: 'active', accent_color: null },
	{ id: 'p2', name: 'Personal', status: 'active', accent_color: null },
];

describe('buildResumeThreads', () => {
	/**
	 * Supersedes an earlier assertion that ranked activity state ahead of
	 * recency. That ordering is the reported defect: a surface named Activity
	 * showed older rows above newer ones. State is now a mark only.
	 */
	it('orders by recency alone, keeping state as a mark, and omits hidden children', () => {
		const rows = [
			thread('recent', '2026-08-14T12:00:00Z'),
			thread('stale-active', '2026-08-12T12:00:00Z'),
			thread('stale-unread', '2026-08-13T12:00:00Z'),
			thread('child', '2026-08-15T12:00:00Z', { spawned_by: 'stale-active' }),
			thread('archived', '2026-08-16T12:00:00Z', { archived: 1 }),
		];
		const result = buildResumeThreads(rows, {
			'stale-active': 'active',
			'stale-unread': 'done',
		});
		expect(result.map((item) => item.id)).toEqual(['recent', 'stale-unread', 'stale-active']);
		// The marks survive; only their ordering power is gone.
		expect(result.map((item) => item.activity)).toEqual([null, 'done', 'active']);
	});

	it('does not let a stale active flag outrank a thread touched minutes ago', () => {
		const rows = [
			thread('wedged', '2026-08-01T09:00:00Z'),
			thread('just-touched', '2026-08-16T18:30:00Z'),
		];
		const result = buildResumeThreads(rows, { wedged: 'active' });
		expect(result.map((item) => item.id)).toEqual(['just-touched', 'wedged']);
	});

	it('does not demote a thread that simply has no live signal', () => {
		const rows = [
			thread('no-signal', '2026-08-16T12:00:00Z'),
			thread('has-done', '2026-08-15T12:00:00Z'),
		];
		const result = buildResumeThreads(rows, { 'has-done': 'done' });
		expect(result.map((item) => item.id)).toEqual(['no-signal', 'has-done']);
	});

	it('breaks equal-recency ties on a stable id so order never flickers', () => {
		const rows = [
			thread('b-thread', '2026-08-16T12:00:00Z'),
			thread('a-thread', '2026-08-16T12:00:00Z'),
		];
		const once = buildResumeThreads(rows, {}).map((item) => item.id);
		const twice = buildResumeThreads([...rows].reverse(), {}).map((item) => item.id);
		expect(once).toEqual(['a-thread', 'b-thread']);
		expect(twice).toEqual(once);
	});

	it('limits project Activity to that project while workspace Activity spans projects', () => {
		const rows = [
			thread('p1', '2026-08-14T12:00:00Z', { project_id: 'p1' }),
			thread('p2', '2026-08-15T12:00:00Z', { project_id: 'p2' }),
			thread('loose', '2026-08-16T12:00:00Z'),
		];
		expect(buildResumeThreads(rows, {}, { projectId: 'p1' }).map((item) => item.id)).toEqual(['p1']);
		expect(buildResumeThreads(rows, {}).map((item) => item.id)).toEqual(['loose', 'p2', 'p1']);
	});
});

describe('buildActivityGroups', () => {
	it('groups workspace rollups by project plus a distinct Workspace group', () => {
		const entries = [
			plan('scope', null, null, 'active', 0, 0, 'idle'),
			plan('project-scope', null, 'p1', 'active', 0, 0, 'idle'),
			plan('doing', 'thread-1', 'p1', 'active', 1, 3, 'advancing'),
			plan('done', 'thread-2', 'p1', 'active', 2, 2, 'idle'),
			plan('loose', 'thread-3', null, 'paused', 0, 1, 'idle'),
		];
		const threads = [
			thread('thread-1', '2026-08-14T12:00:00Z', {
				project_id: 'p1',
				agent_enabled: 1,
				agent_status: 'active',
				agent_responsibility: 'Own plans',
			}),
			thread('thread-2', '2026-08-13T12:00:00Z', { project_id: 'p1' }),
			thread('thread-3', '2026-08-12T12:00:00Z'),
		];
		const groups = buildActivityGroups(entries, threads, PROJECTS);
		expect(groups.map((group) => group.label)).toEqual(['CORSIA', 'Workspace']);
		expect(groups[0].current.map((row) => row.entry.plan.id)).toEqual(['doing']);
		expect(groups[0].complete.map((row) => row.entry.plan.id)).toEqual(['done']);
		expect(groups[0].current[0].ownerType).toBe('agent');
		expect(groups[1].waiting[0].state).toBe('waiting');
	});

	it('uses thread and agent rows directly inside a project Activity view', () => {
		const entries = [
			plan('p1-work', 'thread-1', 'p1', 'active', 0, 2, 'blocked'),
			plan('p2-work', 'thread-2', 'p2', 'active', 0, 2, 'advancing'),
		];
		const threads = [
			thread('thread-1', '2026-08-14T12:00:00Z', { project_id: 'p1' }),
			thread('thread-2', '2026-08-14T12:00:00Z', { project_id: 'p2' }),
		];
		const groups = buildActivityGroups(entries, threads, PROJECTS, { projectId: 'p1' });
		expect(groups).toHaveLength(1);
		expect(groups[0].label).toBe('CORSIA');
		expect(groups[0].waiting.map((row) => row.entry.plan.id)).toEqual(['p1-work']);
	});
});

describe('archived projects', () => {
	const catalog: ProjectInfo[] = [
		...PROJECTS,
		{ id: 'gone', name: 'Gone', status: 'archived', accent_color: null },
	];

	it('does not offer a thread in an archived project to continue', () => {
		const rows = [
			thread('live', '2026-08-14T12:00:00Z', { project_id: 'p1' }),
			thread('stale', '2026-08-15T12:00:00Z', { project_id: 'gone' }),
			thread('loose', '2026-08-13T12:00:00Z'),
		];
		expect(
			buildResumeThreads(rows, {}, { projects: catalog }).map((item) => item.id),
		).toEqual(['live', 'loose']);
		expect(buildResumeThreads(rows, {}).map((item) => item.id)).toEqual(['stale', 'live', 'loose']);
	});

	it('renders every row the server projection supplies', () => {
		const entries = [
			plan('live-work', 'thread-1', 'p1', 'active', 1, 3, 'advancing'),
			plan('stale-work', 'thread-2', 'gone', 'active', 1, 3, 'advancing'),
		];
		const threads = [
			thread('thread-1', '2026-08-14T12:00:00Z', { project_id: 'p1' }),
			thread('thread-2', '2026-08-14T12:00:00Z', { project_id: 'gone' }),
		];
		const groups = buildActivityGroups(entries, threads, catalog);
		expect(groups.map((group) => group.id)).toEqual(['p1', 'gone']);
	});
});

describe('buildActivityGroups ordering', () => {
	const NOW = Date.parse('2026-08-16T18:00:00Z');
	const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();
	const MINUTE = 60 * 1000;

	function rollup(entries: ActivityIndexRow[], now = NOW) {
		const threads = entries.map((entry) =>
			thread(entry.owner.thread_id!, '2026-08-14T12:00:00Z', { project_id: 'p1' }));
		const groups = buildActivityGroups(entries, threads, PROJECTS, { now });
		// In progress first, then Waiting: the order a reader meets the rows in.
		return [...groups[0].current, ...groups[0].waiting].map((row) => row.entry.plan.id);
	}

	/**
	 * Supersedes an earlier assertion that ordered advancing rows purely by
	 * recency against idle rows. With truncation that buries live progress under
	 * recently-touched idle plans, so advancing is its own tier above the rest.
	 */
	it('puts in-flight work above non-actionable work regardless of recency', () => {
		expect(rollup([
			plan('advancing-old', 't1', 'p1', 'active', 0, 3, 'advancing', iso(90 * MINUTE)),
			plan('idle-fresh', 't2', 'p1', 'paused', 0, 3, 'idle', iso(5 * MINUTE)),
		])).toEqual(['advancing-old', 'idle-fresh']);
	});

	it('orders within the non-actionable tier by recency alone', () => {
		expect(rollup([
			plan('blocked-old', 't1', 'p1', 'active', 0, 3, 'blocked', iso(90 * MINUTE)),
			plan('paused-fresh', 't2', 'p1', 'paused', 0, 3, 'idle', iso(5 * MINUTE)),
		])).toEqual(['paused-fresh', 'blocked-old']);
	});

	it('promotes a recently changed actionable row above fresher non-actionable work', () => {
		expect(rollup([
			plan('advancing', 't1', 'p1', 'active', 0, 3, 'advancing', iso(1 * MINUTE)),
			withUserAction(plan('blocked', 't2', 'p1', 'active', 0, 3, 'blocked', iso(30 * MINUTE))),
			withReview(
				plan('your-review', 't3', 'p1', 'active', 0, 3, 'needs-review', iso(45 * MINUTE)),
				'human_review',
			),
		])).toEqual(['blocked', 'your-review', 'advancing']);
	});

	it('decays a promotion once the actionable row goes quiet', () => {
		const entries = [
			plan('advancing', 't1', 'p1', 'active', 0, 3, 'advancing', iso(10 * MINUTE)),
			withUserAction(plan('blocked', 't2', 'p1', 'active', 0, 3, 'blocked', iso(PROMOTION_QUIET_WINDOW_MS))),
		];
		// Inside the window the blocker still leads; one minute past it, recency wins.
		expect(rollup(entries, NOW)).toEqual(['blocked', 'advancing']);
		expect(rollup(entries, NOW + MINUTE)).toEqual(['advancing', 'blocked']);
	});

	/**
	 * A `parent_agent` review is routine agent completion, not the user's move,
	 * so it orders by recency like any other non-actionable row. Promoting it
	 * would turn Activity into the manual curation queue the product decision
	 * forbids.
	 */
	it('never promotes an agent review however recently it changed', () => {
		expect(rollup([
			withReview(
				plan('agent-review', 't1', 'p1', 'active', 0, 3, 'needs-review', iso(3 * MINUTE)),
				'parent_agent',
			),
			withUserAction(plan('blocked-older', 't2', 'p1', 'active', 0, 3, 'blocked', iso(20 * MINUTE))),
		])).toEqual(['blocked-older', 'agent-review']);
	});

	it('breaks equal-recency ties on plan id so order never flickers', () => {
		const entries = [
			plan('b-plan', 't1', 'p1', 'active', 0, 3, 'advancing', iso(MINUTE)),
			plan('a-plan', 't2', 'p1', 'active', 0, 3, 'advancing', iso(MINUTE)),
		];
		expect(rollup(entries)).toEqual(['a-plan', 'b-plan']);
		expect(rollup([...entries].reverse())).toEqual(['a-plan', 'b-plan']);
	});

	/** A named hard requirement, previously uncovered: external is not the user's move. */
	it('never promotes an external review however recently it changed', () => {
		expect(rollup([
			withReview(
				plan('external', 't1', 'p1', 'active', 0, 3, 'needs-review', iso(2 * MINUTE)),
				'external',
			),
			withUserAction(plan('blocked-older', 't2', 'p1', 'active', 0, 3, 'blocked', iso(30 * MINUTE))),
		])).toEqual(['blocked-older', 'external']);
	});

	/**
	 * Decay uses the server-selected actionable timestamp, not plan activity —
	 * otherwise child churn would keep a stale blocker promoted forever.
	 */
	it('decays on compact priority_at, not unrelated plan activity', () => {
		const entry = plan('busy-blocked', 't1', 'p1', 'active', 0, 3, 'blocked', iso(MINUTE));
		entry.priority_at = iso(9 * 60 * MINUTE);
		expect(rollup([
			entry,
			plan('advancing', 't2', 'p1', 'active', 0, 3, 'advancing', iso(30 * MINUTE)),
		])).toEqual(['advancing', 'busy-blocked']);
	});

	it('does not refresh human-review promotion from newer unrelated activity', () => {
		const entry = withReview(
			plan('mixed-review', 't1', 'p1', 'active', 0, 3, 'needs-review', iso(MINUTE)),
			'human_review',
		);
		entry.priority_at = iso(9 * 60 * MINUTE);
		expect(rollup([
			entry,
			plan('advancing', 't2', 'p1', 'active', 0, 3, 'advancing', iso(30 * MINUTE)),
		])).toEqual(['advancing', 'mixed-review']);
	});

	/**
	 * The rollup truncates, so ordering decides what is seen without expanding.
	 * Idle, paused and agent-review rows must not consume a visible slot while
	 * actionable or in-flight work is hidden behind the cut.
	 */
	it('never lets quiet work hold a visible slot while live work is truncated away', () => {
		const order = rollup([
			plan('idle-1', 't1', 'p1', 'active', 0, 3, 'idle', iso(MINUTE)),
			plan('idle-2', 't2', 'p1', 'active', 0, 3, 'idle', iso(2 * MINUTE)),
			plan('paused-1', 't3', 'p1', 'paused', 0, 3, 'idle', iso(3 * MINUTE)),
			withReview(
				plan('agent-1', 't4', 'p1', 'active', 0, 3, 'needs-review', iso(4 * MINUTE)),
				'parent_agent',
			),
			plan('advancing-1', 't5', 'p1', 'active', 0, 3, 'advancing', iso(50 * MINUTE)),
			withUserAction(plan('blocked-1', 't6', 'p1', 'active', 0, 3, 'blocked', iso(60 * MINUTE))),
			plan('advancing-2', 't7', 'p1', 'active', 0, 3, 'advancing', iso(70 * MINUTE)),
		]);
		// Four is the default visible slice in ActivityView.
		const aboveFold = order.slice(0, 4);
		// All three live rows fit, so slot 4 legitimately goes to the freshest
		// quiet row; the guarantee is that quiet work never displaces live work.
		expect(aboveFold).toEqual(['blocked-1', 'advancing-1', 'advancing-2', 'idle-1']);
		for (const quiet of ['paused-1', 'agent-1']) {
			expect(aboveFold).not.toContain(quiet);
		}
	});

	it('fills every visible slot with live work when live work exceeds the fold', () => {
		const order = rollup([
			plan('idle-fresh', 't1', 'p1', 'active', 0, 3, 'idle', iso(MINUTE)),
			plan('paused-fresh', 't2', 'p1', 'paused', 0, 3, 'idle', iso(MINUTE)),
			withReview(
				plan('agent-fresh', 't3', 'p1', 'active', 0, 3, 'needs-review', iso(MINUTE)),
				'parent_agent',
			),
			withUserAction(plan('blocked-1', 't4', 'p1', 'active', 0, 3, 'blocked', iso(40 * MINUTE))),
			plan('advancing-1', 't5', 'p1', 'active', 0, 3, 'advancing', iso(50 * MINUTE)),
			plan('advancing-2', 't6', 'p1', 'active', 0, 3, 'advancing', iso(60 * MINUTE)),
			plan('advancing-3', 't7', 'p1', 'active', 0, 3, 'advancing', iso(70 * MINUTE)),
		]);
		expect(order.slice(0, 4)).toEqual([
			'blocked-1', 'advancing-1', 'advancing-2', 'advancing-3',
		]);
		// Nothing is filtered out — the quiet rows are still present, just below.
		expect(order).toHaveLength(7);
	});
});

describe('buildActivityGroups sections', () => {
	type Label = 'working' | 'needs_you' | 'waiting' | 'finished';
	const TONES = {
		working: 'active', needs_you: 'human-review', waiting: 'waiting', finished: 'complete',
	} as const;

	/** A row with the server label chosen directly, as the projection sends it. */
	function labeled(
		id: string,
		label: Label | 'unrecognized',
		over: { planState?: string; liveness?: 'advancing' | 'idle' | 'paused' | 'waiting' } = {},
	): ActivityIndexRow {
		const base = plan(id, `t-${id}`, 'p1', over.planState ?? 'active', 0, 2, over.liveness ?? 'idle');
		const key = label as Label;
		return {
			...base,
			display: { label_key: key, tone_key: TONES[key] ?? 'waiting' } as ActivityIndexRow['display'],
		};
	}

	function sections(entries: ActivityIndexRow[]) {
		const threads = entries.map((entry) =>
			thread(entry.owner.thread_id!, '2026-08-14T12:00:00Z', { project_id: 'p1' }));
		const [group] = buildActivityGroups(entries, threads, PROJECTS);
		const ids = (rows: { entry: ActivityIndexRow }[]) => rows.map((row) => row.entry.plan.id).sort();
		return {
			group,
			current: ids(group.current),
			waiting: ids(group.waiting),
			complete: ids(group.complete),
		};
	}

	it('keeps only working and needs-you rows in progress', () => {
		const result = sections([
			labeled('working', 'working', { liveness: 'advancing' }),
			labeled('needs', 'needs_you'),
			labeled('waiting', 'waiting', { liveness: 'waiting' }),
			labeled('unknown', 'unrecognized'),
			labeled('done', 'finished'),
		]);
		expect(result.current).toEqual(['needs', 'working']);
		expect(result.waiting).toEqual(['unknown', 'waiting']);
		expect(result.complete).toEqual(['done']);
	});

	it('moves a paused plan to waiting however the server labeled it', () => {
		const result = sections([
			labeled('paused-liveness', 'waiting', { liveness: 'paused' }),
			labeled('paused-state', 'waiting', { planState: 'paused' }),
			// A paused plan can still carry a needs-you label (pause contract rule 5).
			labeled('paused-needs-you', 'needs_you', { planState: 'paused' }),
			labeled('paused-liveness-working', 'working', { liveness: 'paused' }),
		]);
		expect(result.current).toEqual([]);
		expect(result.waiting).toEqual([
			'paused-liveness', 'paused-liveness-working', 'paused-needs-you', 'paused-state',
		]);
	});

	it('never counts or promotes paused work as in progress', () => {
		const NOW = Date.parse('2026-08-16T18:00:00Z');
		const fresh = new Date(NOW - 60 * 1000).toISOString();
		const paused = {
			...labeled('paused-needs-you', 'needs_you', { planState: 'paused' }),
			priority_at: fresh,
		};
		const entries = [paused, labeled('moving', 'working', { liveness: 'advancing' })];
		const threads = entries.map((entry) =>
			thread(entry.owner.thread_id!, '2026-08-14T12:00:00Z', { project_id: 'p1' }));
		const [group] = buildActivityGroups(entries, threads, PROJECTS, { now: NOW });
		expect(group.summary).toEqual({ moving: 1, needsYou: 0, waiting: 1, left: 2 });
		expect(rowTier(group.waiting[0], NOW)).toBe(3);
		expect(nextPromotionExpiry([group], NOW)).toBeNull();
	});

	it('leaves an all-waiting group with nothing in progress', () => {
		const result = sections([labeled('w', 'waiting', { liveness: 'waiting' })]);
		expect(result.current).toEqual([]);
		expect(result.waiting).toEqual(['w']);
	});
});

describe('buildActivityGroups summary', () => {
	function summaryFor(entries: ActivityIndexRow[]) {
		const threads = entries.map((entry) =>
			thread(entry.owner.thread_id!, '2026-08-14T12:00:00Z', { project_id: 'p1' }));
		return buildActivityGroups(entries, threads, PROJECTS)[0].summary;
	}

	it('counts each state class separately instead of one "active" total', () => {
		expect(summaryFor([
			plan('moving', 't1', 'p1', 'active', 1, 4, 'advancing'),
			plan('idle', 't2', 'p1', 'active', 0, 2, 'idle'),
			plan('paused', 't3', 'p1', 'paused', 0, 2, 'idle'),
			plan('blocked', 't4', 'p1', 'active', 0, 3, 'blocked'),
			withReview(plan('yours', 't5', 'p1', 'active', 2, 5, 'needs-review'), 'human_review'),
		])).toEqual({ moving: 1, needsYou: 1, waiting: 2, left: 8 });
	});

	it('sums the server\'s open count rather than recounting leaves', () => {
		const summary = summaryFor([
			plan('a', 't1', 'p1', 'active', 7, 13, 'advancing'),
			plan('b', 't2', 'p1', 'active', 2, 2, 'idle'),
		]);
		expect(summary.left).toBe(6);
	});

	it('reports zero for absent classes so the consumer can omit them', () => {
		expect(summaryFor([
			plan('idle', 't1', 'p1', 'active', 0, 0, 'idle'),
		])).toEqual({ moving: 0, needsYou: 0, waiting: 0, left: 0 });
	});
});

describe('buildActivityGroups project navigation', () => {
	it('exposes the project id a group header navigates into', () => {
		const entries = [plan('work', 'thread-1', 'p2', 'active', 0, 2, 'advancing')];
		const threads = [thread('thread-1', '2026-08-14T12:00:00Z', { project_id: 'p2' })];
		const groups = buildActivityGroups(entries, threads, PROJECTS);
		expect(groups[0].label).toBe('Personal');
		expect(groups[0].projectId).toBe('p2');
	});

	it('gives the Workspace group no project id, so it offers no jump target', () => {
		const entries = [plan('loose', 'thread-1', null, 'active', 0, 2, 'advancing')];
		const threads = [thread('thread-1', '2026-08-14T12:00:00Z')];
		const groups = buildActivityGroups(entries, threads, PROJECTS);
		expect(groups[0].label).toBe('Workspace');
		expect(groups[0].projectId).toBeNull();
	});

	/**
	 * A project with no plan work produces no group, so headers alone cannot
	 * reach every project. That is why Activity also carries one palette
	 * affordance; without it these projects would be unreachable from here.
	 */
	it('produces no group for a project with no plan work', () => {
		const entries = [plan('work', 'thread-1', 'p1', 'active', 0, 2, 'advancing')];
		const threads = [thread('thread-1', '2026-08-14T12:00:00Z', { project_id: 'p1' })];
		const groups = buildActivityGroups(entries, threads, PROJECTS);
		expect(groups.map((group) => group.projectId)).toEqual(['p1']);
		expect(groups.some((group) => group.projectId === 'p2')).toBe(false);
	});
});
