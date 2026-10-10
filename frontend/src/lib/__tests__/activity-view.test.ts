import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ActivityView from '$lib/components/activity/ActivityView.svelte';
import Sidebar from '$lib/components/Sidebar.svelte';
import { threads, activeThreadId, type Thread } from '$lib/stores/threads';
import { _resetProjectsForTests, _setProjectsForTests, projectLoadError } from '$lib/stores/projects';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { uiScope } from '$lib/stores/uiScope';
import { openThread } from '$lib/stores/openThread';
import { paletteOpen } from '$lib/stores/palette';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { resetPlanRuntimeForTests } from '$lib/stores/plans.svelte';
import { ACTIVITY_MIN_GAP_MS } from '$lib/stores/planRefreshSpacing';
import { resetObserversForTests } from '$lib/stores/planQuery.svelte';
import type {
	ActivityIndexEnvelope,
	ActivityIndexRow,
	OperatorEnvelope,
	OperatorNode,
	OperatorPlanEntry,
} from '$lib/api/plans';
import { PROMOTION_QUIET_WINDOW_MS } from '$lib/activityHome';
import { EMPTY_DISTRIBUTION, operatorNode, planDisplay } from './fixtures/operatorPlan';
import { blockedPlanTree } from './fixtures/blockedPlanTree';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let wsListeners: Map<string, (data: unknown) => void>;

vi.mock('$lib/ws', () => ({
	on: (event: string, cb: (data: unknown) => void) => {
		wsListeners.set(event, cb);
		return () => wsListeners.delete(event);
	},
}));

function thread(id: string, title: string, projectId: string | null, updatedAt: string): Thread {
	return {
		id,
		user_id: 'user-1',
		profile: 'personal',
		title,
		model: null,
		project_id: projectId,
		created_at: updatedAt,
		updated_at: updatedAt,
	};
}

function entry(
	id: string,
	threadId: string | null,
	projectId: string | null,
	done: number,
	total: number,
	liveness: 'advancing' | 'idle' | 'blocked' | 'needs-review' | 'waiting' | 'paused',
	state = 'active',
): ActivityIndexRow {
	return {
		plan: {
			id,
			owner_kind: threadId ? 'thread' : projectId ? 'project_scope' : 'workspace_scope',
			owner_id: threadId ?? projectId ?? 'workspace',
			state,
			revision: 1,
			updated_at: '2026-08-14T12:00:00Z',
		},
		owner: {
			label: threadId ? `Plan for ${threadId}` : projectId ?? 'Workspace',
			project_id: projectId,
			thread_id: threadId,
		},
		progress: { open: total - done, liveness },
		has_nodes: total > 0,
		display: done === total
			? planDisplay('finished', 'complete')
			: liveness === 'advancing' || liveness === 'needs-review' || liveness === 'idle' && state !== 'paused'
				? planDisplay('working', 'active')
				: planDisplay('waiting', 'waiting'),
		priority_at: '2026-08-14T12:00:00Z',
	};
}

function envelope(kind: 'workspace_scope' | 'project_scope'): ActivityIndexEnvelope {
	return {
		contract_version: 1,
		representation: 'activity_scope_index',
		as_of: '2026-08-14T12:00:00Z',
		target: {
			kind,
			id: kind === 'workspace_scope' ? 'workspace' : 'p1',
			label: kind === 'workspace_scope' ? 'Workspace' : 'CORSIA',
		},
		revision_token: '[REDACTED]',
		plans: [
			entry('scope', null, kind === 'project_scope' ? 'p1' : null, 0, 0, 'idle'),
			entry('doing', 'thread-doing', 'p1', 1, 3, 'advancing'),
			entry('done', 'thread-done', 'p1', 2, 2, 'idle'),
			...(kind === 'workspace_scope'
				? [entry('loose', 'thread-loose', null, 0, 1, 'idle', 'paused')]
				: []),
		],
		attention: { awaiting_count: 1 },
	};
}

function fullEntry(
	index: ActivityIndexRow,
	nodes: OperatorNode[] = [],
	currentNodeIds: string[] = [],
	nextNodeIds: string[] = [],
): OperatorPlanEntry {
	return {
		plan: { ...index.plan },
		owner: { ...index.owner },
		progress: {
			done: 0,
			total: index.progress.open,
			open: index.progress.open,
			liveness: index.progress.liveness,
			distribution: { ...EMPTY_DISTRIBUTION },
		},
		display: index.display,
		oversize: null,
		current_node_ids: currentNodeIds,
		next_node_ids: nextNodeIds,
		nodes,
	};
}

function detailEnvelope(index: ActivityIndexRow, entryOverride?: OperatorPlanEntry): OperatorEnvelope {
	return {
		contract_version: 2,
		as_of: '2026-08-14T12:00:00Z',
		target: { kind: 'thread', id: index.owner.thread_id!, label: index.owner.label },
		revision_token: 'detail-revision',
		plans: [entryOverride ?? fullEntry(index)],
		attention: { awaiting_count: 0 },
	};
}

function response(payload: unknown, status = 200, etag = '"activity"') {
	return {
		ok: status >= 200 && status < 300,
		status,
		json: async () => payload,
		headers: { get: (key: string) => key === 'ETag' ? etag : null },
	};
}

function mockFetch(payload: ActivityIndexEnvelope | null, status = 200) {
	vi.stubGlobal('fetch', vi.fn(async () => ({
		ok: status >= 200 && status < 300,
		status,
		json: async () => payload,
		headers: { get: (key: string) => key === 'ETag' ? '"activity"' : null },
	})));
}

async function settle() {
	await tick();
	flushSync();
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

function branchToggleIn(container: Element, title: string): Element | null {
	const row = [...container.querySelectorAll('.tree-row')]
		.find((item) => item.querySelector('.row-title')?.textContent === title);
	return row?.querySelector('.branch-toggle') ?? null;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	wsListeners = new Map();
	resetPlanRuntimeForTests();
	resetObserversForTests();
	token.set('test-bearer');
	connectionState.set('connected');
	activeThreadId.set(null);
	uiScope.set(null);
	openThread.set(null);
	paletteOpen.set(false);
	_resetTurnStateForTests();
	_setProjectsForTests([
		{ id: 'p1', name: 'CORSIA', status: 'active', accent_color: null },
	]);
	threads.set([
		thread('thread-doing', 'Doing thread', 'p1', '2026-08-14T12:00:00Z'),
		thread('thread-done', 'Done thread', 'p1', '2026-08-13T12:00:00Z'),
		thread('thread-loose', 'Loose thread', null, '2026-08-12T12:00:00Z'),
	]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	resetObserversForTests();
	_resetProjectsForTests();
	vi.unstubAllGlobals();
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe('ActivityView', () => {
	it('puts Continue first, then a grouped workspace rollup with completed work collapsed', async () => {
		mockFetch(envelope('workspace_scope'));
		const onSelectThread = vi.fn();
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread },
		});
		await settle();

		expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/plans/activity/scopes/workspace');
		const text = host.textContent ?? '';
		expect(text.indexOf('Continue')).toBeLessThan(text.indexOf('In progress'));
		expect(host.querySelector('h1')?.textContent).toBe('Activity');
		expect(text).toContain('CORSIA');
		expect(text).toContain('Workspace');
		expect(text).toContain('Needs you');
		expect(host.querySelector('.needs-you')?.textContent).toContain('decision');
		expect(host.querySelector('.needs-you')?.textContent).not.toContain('item needs your input');
		expect(host.querySelector('.resume-mark svg')).toBeTruthy();
		expect(host.querySelector('.owner-mark svg')).toBeTruthy();
		expect(host.textContent).not.toContain('Plan for thread-done');

		const complete = host.querySelector('button.complete-toggle') as HTMLButtonElement;
		expect(complete.textContent).toContain('Complete (1)');
		expect(complete.closest('h2')?.id).toBe('activity-complete-title');
		complete.click();
		await settle();
		expect(host.querySelector('.complete-work')?.textContent).toContain('Plan for thread-done');

		const resume = [...host.querySelectorAll('button')].find((button) =>
			button.textContent?.includes('Doing thread'),
		) as HTMLButtonElement;
		resume.click();
		expect(onSelectThread).toHaveBeenCalledWith('thread-doing');
	});

	it('keeps waiting and paused plans out of In progress, in a collapsed Waiting section', async () => {
		const payload = envelope('workspace_scope');
		for (let i = 0; i < 5; i += 1) {
			payload.plans.push(entry(`stuck-${i}`, `thread-stuck-${i}`, 'p1', 0, 2, 'blocked'));
		}
		mockFetch(payload);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		const progress = host.querySelector('.activity-work:not(.waiting-work)')?.textContent ?? '';
		expect(progress).toContain('Plan for thread-doing');
		expect(progress).not.toContain('Plan for thread-loose');
		expect(progress).not.toContain('Plan for thread-stuck-0');

		// Collapsed by default, and the count is the number of waiting rows (5 blocked + 1 paused).
		const toggle = host.querySelector('button.waiting-toggle') as HTMLButtonElement;
		expect(toggle.textContent).toContain('Waiting (6)');
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(host.querySelector('.waiting-work')?.textContent).not.toContain('Plan for thread-loose');

		toggle.click();
		await settle();
		const waiting = host.querySelector('.waiting-work')?.textContent ?? '';
		expect(waiting).toContain('Plan for thread-loose');
		// Each group keeps the four-row limit with its own "Show N more": 4 of the
		// project's 5 plus the Workspace plan.
		expect(waiting).toContain('Show 1 more');
		expect(host.querySelectorAll('.waiting-work .row-summary')).toHaveLength(5);
	});

	it('reactively decays a promoted user ask while Activity remains open', async () => {
		vi.useFakeTimers();
		const now = Date.parse('2026-08-16T18:00:00Z');
		vi.setSystemTime(now);
		const updated = (msAgo: number) => new Date(now - msAgo).toISOString();
		const moving = entry('moving', 'thread-moving', 'p1', 0, 3, 'advancing');
		moving.plan.updated_at = updated(30 * 60 * 1000);
		const blocked = entry('blocked', 'thread-blocked', 'p1', 0, 3, 'blocked');
		blocked.plan.updated_at = updated(60 * 60 * 1000);
		blocked.display = planDisplay('needs_you', 'human-review');
		blocked.priority_at = updated(PROMOTION_QUIET_WINDOW_MS - 10);
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], moving, blocked];
		threads.set([
			thread('thread-moving', 'Moving thread', 'p1', moving.plan.updated_at),
			thread('thread-blocked', 'Blocked thread', 'p1', blocked.plan.updated_at),
		]);
		mockFetch(payload);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await vi.advanceTimersByTimeAsync(0);
		await tick();
		flushSync();

		const order = () => [...host.querySelectorAll('.group-rows > .plan-row .owner-copy strong')]
			.map((node) => node.textContent?.trim());
		expect(order()).toEqual(['Plan for thread-blocked', 'Plan for thread-moving']);

		await vi.advanceTimersByTimeAsync(11);
		await tick();
		flushSync();
		expect(order()).toEqual(['Plan for thread-moving', 'Plan for thread-blocked']);
	});

	it('loads full thread detail only on expansion and renders the existing tree', async () => {
		const shape = blockedPlanTree();
		const blocked = entry('blocked', 'thread-blocked', 'p1', 1, 4, 'blocked');
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], blocked];
		const detail = detailEnvelope(
			blocked,
			fullEntry(blocked, shape.nodes, shape.currentNodeIds, shape.nextNodeIds),
		);
		threads.set([thread('thread-blocked', 'Blocked thread', 'p1', '2026-08-14T12:00:00Z')]);
		const fetchMock = vi.fn(async (url: string) => String(url).includes('/operator/threads/')
			? response(detail, 200, '"detail"')
			: response(payload));
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
			'/api/plans/activity/scopes/workspace',
		]);
		// A blocked plan is not in progress: it sits in the collapsed Waiting section.
		expect(host.querySelector('.row-summary[aria-label="Expand Plan for thread-blocked"]')).toBeNull();
		(host.querySelector('.waiting-toggle') as HTMLButtonElement).click();
		flushSync();
		const summary = host.querySelector('.row-summary[aria-label="Expand Plan for thread-blocked"]') as HTMLButtonElement;
		expect(summary).toBeTruthy();
		summary.focus();
		summary.click();
		flushSync();
		expect(summary.getAttribute('aria-expanded')).toBe('true');
		expect(document.activeElement).toBe(summary);
		expect(host.querySelector('.detail-loading')?.getAttribute('aria-busy')).toBe('true');
		await settle();

		expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
			'/api/plans/activity/scopes/workspace',
			'/api/plans/operator/threads/thread-blocked',
		]);
		const row = summary.closest('.plan-row') as HTMLElement;
		expect(branchToggleIn(row, 'Build')?.getAttribute('aria-expanded')).toBe('true');
		expect(branchToggleIn(row, 'Docs')?.getAttribute('aria-expanded')).toBe('false');
		expect(branchToggleIn(row, 'Polish')?.getAttribute('aria-expanded')).toBe('false');
		// Settled work is always visible; there is no separate toggle to hide it.
		expect(row.textContent).toContain('Kickoff');
		expect(row.textContent).not.toContain('Hide completed');
	});

	it('opens a row without nodes instead of issuing a detail read', async () => {
		const emptyRow = entry('empty', 'thread-empty', 'p1', 0, 0, 'idle');
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], emptyRow];
		threads.set([thread('thread-empty', 'Empty thread', 'p1', '2026-08-14T12:00:00Z')]);
		mockFetch(payload);
		const onSelectThread = vi.fn();
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread },
		});
		await settle();
		(host.querySelector('button.complete-toggle') as HTMLButtonElement).click();
		await settle();

		const summary = host.querySelector('.row-summary[aria-label="Open Plan for thread-empty"]') as HTMLButtonElement;
		expect(summary).toBeTruthy();
		// One affordance inside the row-wide tap target: a forward chevron for a row that opens.
		expect(summary.querySelectorAll('.chevron')).toHaveLength(1);
		expect(summary.querySelector('.chevron.opens')).toBeTruthy();
		summary.click();
		await settle();
		expect(onSelectThread).toHaveBeenCalledWith('thread-empty');
		expect(vi.mocked(fetch).mock.calls.map(([url]) => url)).toEqual([
			'/api/plans/activity/scopes/workspace',
		]);
	});

	it('keeps a detail failure local and moves Retry focus into the loaded tree', async () => {
		const failed = entry('failed-detail', 'thread-failed', 'p1', 0, 2, 'idle');
		const sibling = entry('sibling', 'thread-sibling', 'p1', 0, 1, 'idle');
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], failed, sibling];
		threads.set([
			thread('thread-failed', 'Failed thread', 'p1', '2026-08-14T12:00:00Z'),
			thread('thread-sibling', 'Sibling thread', 'p1', '2026-08-13T12:00:00Z'),
		]);
		let detailReads = 0;
		const shape = blockedPlanTree();
		const detail = detailEnvelope(
			failed,
			fullEntry(failed, shape.nodes, shape.currentNodeIds, shape.nextNodeIds),
		);
		const fetchMock = vi.fn(async (url: string) => {
			if (!String(url).includes('/operator/threads/')) return response(payload);
			detailReads += 1;
			return detailReads === 1 ? response(null, 500) : response(detail, 200, '"detail"');
		});
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		const summary = host.querySelector('.row-summary[aria-label="Expand Plan for thread-failed"]') as HTMLButtonElement;
		summary.click();
		await settle();
		const row = summary.closest('.plan-row') as HTMLElement;
		expect(row.textContent).toContain("Couldn't load this plan.");
		expect(row.textContent).toContain('Open');
		expect(host.querySelector('.row-summary[aria-label="Expand Plan for thread-sibling"]')?.getAttribute('aria-expanded')).toBe('false');

		const retry = [...row.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry') as HTMLButtonElement;
		retry.focus();
		retry.click();
		await settle();
		expect(detailReads).toBe(2);
		expect(document.activeElement).toBe(row.querySelector('button.row-body'));
	});

	it('keeps stale detail visible and makes failed refreshes recoverable', async () => {
		const indexed = entry('stale-detail', 'thread-stale-detail', 'p1', 0, 1, 'idle');
		indexed.plan.revision = 2;
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], indexed];
		threads.set([thread('thread-stale-detail', 'Stale detail', 'p1', '2026-08-14T12:00:00Z')]);
		const shape = blockedPlanTree();
		const oldEntry = fullEntry(indexed, shape.nodes, shape.currentNodeIds, shape.nextNodeIds);
		oldEntry.plan.revision = 1;
		const currentEntry = fullEntry(indexed, shape.nodes, shape.currentNodeIds, shape.nextNodeIds);
		let detailReads = 0;
		const fetchMock = vi.fn(async (url: string) => {
			if (!String(url).includes('/operator/threads/')) return response(payload);
			detailReads += 1;
			if (detailReads === 1) return response(detailEnvelope(indexed, oldEntry), 200, '"old"');
			if (detailReads === 4) return response(detailEnvelope(indexed, currentEntry), 200, '"current"');
			return response(null, 500);
		});
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		const summary = host.querySelector('.row-summary[aria-label="Expand Plan for thread-stale-detail"]') as HTMLButtonElement;
		summary.click();
		await settle();
		await settle();
		const row = summary.closest('.plan-row') as HTMLElement;
		expect(detailReads).toBe(2);
		expect(row.querySelector('button.row-body')).toBeTruthy();
		expect(row.textContent).toContain("Couldn't load this plan.");
		expect(row.textContent).toContain('Open');

		let retry = [...row.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry') as HTMLButtonElement;
		retry.focus();
		retry.click();
		await settle();
		expect(detailReads).toBe(3);
		expect(row.textContent).toContain("Couldn't load this plan.");
		expect(document.activeElement).toBe(retry);

		retry = [...row.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry') as HTMLButtonElement;
		retry.focus();
		retry.click();
		await settle();
		expect(detailReads).toBe(4);
		expect(row.textContent).not.toContain("Couldn't load this plan.");
		expect(document.activeElement).toBe(row.querySelector('button.row-body'));
	});

	it('discards a cold detail response when the row collapses', async () => {
		const lazy = entry('lazy', 'thread-lazy', 'p1', 0, 1, 'idle');
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], lazy];
		threads.set([thread('thread-lazy', 'Lazy thread', 'p1', '2026-08-14T12:00:00Z')]);
		const shape = blockedPlanTree();
		const detail = detailEnvelope(
			lazy,
			fullEntry(lazy, shape.nodes, shape.currentNodeIds, shape.nextNodeIds),
		);
		const detailResolvers: Array<() => void> = [];
		const fetchMock = vi.fn((url: string) => {
			if (!String(url).includes('/operator/threads/')) return Promise.resolve(response(payload));
			return new Promise((resolve) => detailResolvers.push(() => resolve(response(detail, 200, '"detail"'))));
		});
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		const summary = host.querySelector('.row-summary[aria-label="Expand Plan for thread-lazy"]') as HTMLButtonElement;
		summary.click();
		flushSync();
		expect(detailResolvers).toHaveLength(1);
		summary.click();
		flushSync();
		detailResolvers[0]();
		await settle();
		expect(summary.getAttribute('aria-expanded')).toBe('false');

		summary.click();
		flushSync();
		expect(detailResolvers).toHaveLength(2);
		detailResolvers[1]();
		await settle();
		expect(summary.closest('.plan-row')?.querySelector('button.row-body')).toBeTruthy();
	});

	it('revalidates the same stale revision again after collapse and reopen', async () => {
		const indexed = entry('revised', 'thread-revised', 'p1', 0, 1, 'idle');
		indexed.plan.revision = 2;
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], indexed];
		threads.set([thread('thread-revised', 'Revised thread', 'p1', '2026-08-14T12:00:00Z')]);
		const oldEntry = fullEntry(indexed);
		oldEntry.plan.revision = 1;
		const currentEntry = fullEntry(indexed);
		let detailReads = 0;
		const fetchMock = vi.fn(async (url: string) => {
			if (!String(url).includes('/operator/threads/')) return response(payload);
			detailReads += 1;
			return response(
				detailEnvelope(indexed, detailReads === 4 ? currentEntry : oldEntry),
				200,
				`"detail-${detailReads}"`,
			);
		});
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();
		const summary = host.querySelector('.row-summary[aria-label="Expand Plan for thread-revised"]') as HTMLButtonElement;
		summary.click();
		await settle();
		await settle();
		expect(detailReads).toBe(2);

		summary.click();
		await settle();
		summary.click();
		await settle();
		await settle();
		expect(detailReads).toBe(4);
	});

	it('does not fetch collapsed child detail for a child-owned plan change', async () => {
		vi.useFakeTimers({ toFake: ['Date'] });
		const hostIndex = entry('host-plan', 'thread-host', 'p1', 0, 1, 'idle');
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], hostIndex];
		threads.set([thread('thread-host', 'Host thread', 'p1', '2026-08-14T12:00:00Z')]);
		const spawn = operatorNode({
			id: 'spawn',
			title: 'Spawn child',
			child: {
				available: true,
				plan_id: 'child-plan',
				thread_id: 'thread-child',
				progress: { done: 0, total: 1, open: 1, liveness: 'advancing' },
			},
		});
		const hostDetail = detailEnvelope(hostIndex, fullEntry(hostIndex, [spawn]));
		const fetchMock = vi.fn(async (url: string) => String(url).includes('/operator/threads/')
			? response(hostDetail, 200, '"host-detail"')
			: response(payload));
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();
		(host.querySelector('.row-summary[aria-label="Expand Plan for thread-host"]') as HTMLButtonElement).click();
		await settle();
		const before = fetchMock.mock.calls.length;

		// The Activity key spaces its reads; let that gap pass so the nudge reads now.
		vi.setSystemTime(Date.now() + ACTIVITY_MIN_GAP_MS);
		wsListeners.get('plans_changed')?.({
			type: 'plans_changed',
			contract_version: 1,
			plans: [{
				plan_id: 'child-plan', revision: 2,
				owner: { kind: 'thread', id: 'thread-child' },
				scope: { workspace_id: 'workspace', project_id: 'p1' },
			}],
		});
		await settle();
		const urls = fetchMock.mock.calls.slice(before).map(([url]) => String(url));
		expect(urls).toEqual(['/api/plans/activity/scopes/workspace']);
		expect(urls.some((url) => url.includes('/nodes/spawn/child'))).toBe(false);
	});

	it('refreshes expanded child detail and its Activity host for a child-owned change', async () => {
		vi.useFakeTimers({ toFake: ['Date'] });
		const hostIndex = entry('host-plan', 'thread-host', 'p1', 0, 1, 'idle');
		const childIndex = entry('child-plan', 'thread-child', 'p1', 0, 1, 'idle');
		const payload = envelope('workspace_scope');
		payload.plans = [payload.plans[0], hostIndex];
		threads.set([thread('thread-host', 'Host thread', 'p1', '2026-08-14T12:00:00Z')]);
		const spawn = operatorNode({
			id: 'spawn',
			title: 'Spawn child',
			child: {
				available: true,
				plan_id: 'child-plan',
				thread_id: 'thread-child',
				progress: { done: 0, total: 1, open: 1, liveness: 'advancing' },
			},
		});
		const hostDetail = detailEnvelope(hostIndex, fullEntry(hostIndex, [spawn]));
		const childDetail = detailEnvelope(
			childIndex,
			fullEntry(childIndex, [operatorNode({ id: 'child-task', title: 'Child task' })]),
		);
		const fetchMock = vi.fn(async (url: string) => {
			if (String(url).includes('/nodes/spawn/child')) {
				return response(childDetail, 200, '"child-detail"');
			}
			if (String(url).includes('/operator/threads/')) {
				return response(hostDetail, 200, '"host-detail"');
			}
			return response(payload);
		});
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();
		(host.querySelector('.row-summary[aria-label="Expand Plan for thread-host"]') as HTMLButtonElement).click();
		await settle();
		(host.querySelector('.spawn-chip') as HTMLButtonElement).click();
		await settle();
		expect(host.textContent).toContain('Child task');
		const before = fetchMock.mock.calls.length;

		// The Activity key spaces its reads; let that gap pass so the nudge reads now.
		vi.setSystemTime(Date.now() + ACTIVITY_MIN_GAP_MS);
		wsListeners.get('plans_changed')?.({
			type: 'plans_changed',
			contract_version: 1,
			plans: [{
				plan_id: 'child-plan', revision: 2,
				owner: { kind: 'thread', id: 'thread-child' },
				scope: { workspace_id: 'workspace', project_id: 'p1' },
			}],
		});
		await settle();
		const urls = fetchMock.mock.calls.slice(before).map(([url]) => String(url));
		expect(urls).toContain('/api/plans/activity/scopes/workspace');
		expect(urls).toContain('/api/plans/operator/nodes/spawn/child');
		expect(urls).toContain('/api/plans/operator/threads/thread-host');
	});

	it('uses the canonical Agent spark and a distinct thread glyph only in Activity', async () => {
		const agent = {
			...thread('agent-thread', 'Agent thread', 'p1', '2026-08-15T12:00:00Z'),
			agent_enabled: true,
			agent_responsibility: 'Plans',
		};
		threads.set([agent, thread('thread-plain', 'Plain thread', 'p1', '2026-08-14T12:00:00Z')]);
		mockFetch(envelope('workspace_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();
		const marks = [...host.querySelectorAll('.resume-mark svg path')].map((path) => path.getAttribute('d'));
		expect(marks).toContain('M12 3l1.8 5 5 1.8-5 1.8L12 17l-1.8-5-5-1.8 5-1.8z');
		expect(marks.some((path) => path?.startsWith('M20 11.5'))).toBe(true);
	});

	it('loads an empty catalog once when Activity and Sidebar mount together', async () => {
		_resetProjectsForTests();
		const fetchMock = vi.fn(async (input: string) => {
			if (input.startsWith('/api/projects?')) {
				return { ok: true, status: 200, json: async () => ({ projects: [] }) };
			}
			return {
				ok: true,
				status: 200,
				json: async () => envelope('workspace_scope'),
				headers: { get: () => '"activity"' },
			};
		});
		vi.stubGlobal('fetch', fetchMock);
		const sidebar = mount(Sidebar, {
			target: host,
			props: {
				open: true,
				onSelectThread: vi.fn(),
				onDeleteThread: vi.fn(),
				onOpenSettings: vi.fn(),
			},
		});
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		expect(fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/projects?'))).toHaveLength(1);
		unmount(sidebar);
	});

	it('reads project Activity and removes workspace nesting', async () => {
		mockFetch(envelope('project_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: 'p1', scopeName: 'CORSIA', onSelectThread: vi.fn() },
		});
		await settle();

		expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
			'/api/plans/activity/scopes/projects/p1',
		);
		expect(host.querySelectorAll('.activity-group')).toHaveLength(1);
		expect(host.textContent).not.toContain('Plans');
	});

	it('keeps stale payload visible with a quiet status message', async () => {
		mockFetch(envelope('workspace_scope'));
		connectionState.set('disconnected');
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();
		expect(host.textContent).toContain('showing last known activity');
		expect(host.textContent).toContain('Doing thread');
	});

	it('renders a calm empty state only for a successful project rollup with no plans', async () => {
		const empty = { ...envelope('project_scope'), plans: [], attention: { awaiting_count: 0 } };
		mockFetch(empty);
		component = mount(ActivityView, {
			target: host,
			props: { projectId: 'p1', scopeName: 'CORSIA', onSelectThread: vi.fn() },
		});
		await settle();
		expect(host.textContent).toContain('No plan work yet');
		expect(host.textContent).not.toContain('no longer available');
	});

	it('separates a stale project selection from access, with a Workspace recovery action', async () => {
		mockFetch(null, 404);
		const onGoWorkspace = vi.fn();
		component = mount(ActivityView, {
			target: host,
			props: { projectId: 'p1', scopeName: 'CORSIA', onSelectThread: vi.fn(), onGoWorkspace },
		});
		await settle();
		expect(host.textContent).toContain('This selected scope is no longer available');
		expect(host.textContent).not.toContain('You don’t have access');
		const recovery = [...host.querySelectorAll('button')].find((button) =>
			button.textContent?.includes('Back to Workspace'),
		) as HTMLButtonElement;
		recovery.click();
		expect(onGoWorkspace).toHaveBeenCalledOnce();
	});

	it('renders forbidden and server failures as distinct non-empty states', async () => {
		for (const [status, expected] of [
			[403, 'You don’t have access to this scope.'],
			[500, 'Activity couldn’t load'],
		] as const) {
			resetPlanRuntimeForTests();
			resetObserversForTests();
			mockFetch(null, status);
			component = mount(ActivityView, {
				target: host,
				props: { projectId: 'p1', scopeName: 'CORSIA', onSelectThread: vi.fn() },
			});
			await settle();
			expect(host.textContent).toContain(expected);
			unmount(component);
			component = null;
		}
	});
});

describe('ActivityView project navigation', () => {
	it('enters project scope from the existing group label, with no second picker', async () => {
		mockFetch(envelope('workspace_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		const jumps = [...host.querySelectorAll('button.group-jump')];
		expect(jumps.map((button) => button.textContent?.trim())).toEqual(['CORSIA']);

		(jumps[0] as HTMLButtonElement).click();
		await settle();
		expect(get(uiScope)).toMatchObject({ id: 'p1', name: 'CORSIA' });
		expect(get(openThread)).toBeNull();
	});

	it('gives the Workspace group no jump control, because it is not a project', async () => {
		mockFetch(envelope('workspace_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		(host.querySelector('.waiting-toggle') as HTMLButtonElement | null)?.click();
		flushSync();
		const workspaceGroup = [...host.querySelectorAll('.activity-group')].find((group) =>
			group.querySelector('.group-head')?.textContent?.includes('Workspace'),
		);
		expect(workspaceGroup).toBeTruthy();
		expect(workspaceGroup?.querySelector('button.group-jump')).toBeNull();
	});

	/**
	 * A project with no plan work produces no group header, so the palette is the
	 * only route to it from Activity. It reuses the existing palette rather than
	 * adding a second scope-selection owner.
	 */
	it('reaches a zero-plan project through one palette affordance', async () => {
		_setProjectsForTests([
			{ id: 'p1', name: 'CORSIA', status: 'active', accent_color: null },
			{ id: 'p9', name: 'Quiet project', status: 'active', accent_color: null },
		]);
		mockFetch(envelope('workspace_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		expect(host.textContent).not.toContain('Quiet project');
		const all = [...host.querySelectorAll('button.all-projects')];
		expect(all).toHaveLength(1);
		expect(get(paletteOpen)).toBe(false);
		(all[0] as HTMLButtonElement).click();
		expect(get(paletteOpen)).toBe(true);
	});

	it('offers no scope affordances once already inside a project', async () => {
		mockFetch(envelope('project_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: 'p1', scopeName: 'CORSIA', onSelectThread: vi.fn() },
		});
		await settle();

		expect(host.querySelector('button.group-jump')).toBeNull();
		expect(host.querySelector('button.all-projects')).toBeNull();
	});

	/**
	 * The label is passed into the scope and snapshotted there, so offering the
	 * jump before the catalog resolves would persist a placeholder name.
	 */
	it('offers no jump target while the project name is unresolved', async () => {
		_resetProjectsForTests();
		mockFetch(envelope('workspace_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		expect(host.querySelector('button.group-jump')).toBeNull();
		expect(get(uiScope)).toBeNull();
	});
});

describe('ActivityView group summary', () => {
	it('reports each state class instead of one "active" count', async () => {
		mockFetch(envelope('workspace_scope'));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		const summary = host.querySelector('.group-summary')?.textContent?.trim() ?? '';
		// The fixture's project group holds one advancing plan (1/3) and one
		// complete plan (2/2): moving is real, and the ratio spans both.
		expect(summary).toContain('1 moving');
		expect(summary).toContain('2 left');
		// Zero-valued classes are omitted rather than printed.
		expect(summary).not.toContain('0 ');
		expect(summary).not.toContain('needs you');
		expect(summary).not.toContain('blocked');
		// The prohibited count must not reappear anywhere in the header.
		expect(host.querySelector('.group-head')?.textContent).not.toContain('active');
	});

	/**
	 * The catalog is a separate request from the plan rollup. Its failure must be
	 * stated, not absorbed into silently unnamed rows.
	 */
	it('says so when the project catalog fails, while still showing plan work', async () => {
		_resetProjectsForTests();
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (String(url).includes('/api/projects')) {
				return { ok: false, status: 500, json: async () => ({}), headers: { get: () => null } };
			}
			return {
				ok: true, status: 200,
				json: async () => envelope('workspace_scope'),
				headers: { get: (key: string) => (key === 'ETag' ? '"activity"' : null) },
			};
		}));
		component = mount(ActivityView, {
			target: host,
			props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() },
		});
		await settle();

		expect(get(projectLoadError)).toBe('Could not load projects');
		const notes = Array.from(host.querySelectorAll('.stale-note')).map((n) => n.textContent ?? '');
		expect(notes.some((text) => text.includes('Project names couldn’t load'))).toBe(true);
		// Plan rows still render — one failure does not blank the other surface.
		expect(host.querySelectorAll('.group-rows .plan-row').length).toBeGreaterThan(0);
	});
});
