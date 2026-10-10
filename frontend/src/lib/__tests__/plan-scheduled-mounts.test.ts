import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ThreadPlanPane from '$lib/components/plans/ThreadPlanPane.svelte';
import PlanHeadControls from '$lib/components/plans/PlanHeadControls.svelte';
import PlanDrillView from '$lib/components/plans/PlanDrillView.svelte';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import ActivityView from '$lib/components/activity/ActivityView.svelte';
import { threads, activeThreadId } from '$lib/stores/threads';
import { _resetProjectsForTests } from '$lib/stores/projects';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { uiScope } from '$lib/stores/uiScope';
import { resetPlanRuntimeForTests } from '$lib/stores/plans.svelte';
import { resetObserversForTests } from '$lib/stores/planQuery.svelte';
import type { ActivityIndexEnvelope, OperatorEnvelope, OperatorNode } from '$lib/api/plans';
import { EMPTY_DISTRIBUTION, operatorNode, planDisplay } from './fixtures/operatorPlan';
import { CHILD_AVAILABLE, node as treeNode } from './fixtures/planTreeNode';

/**
 * Every place the plan tree mounts must gather scheduled work: the thread pane,
 * the drill view, the Activity plan row and a nested subagent plan. The group is
 * built inside the tree from the nodes' `scheduled_until` and the shared minute clock, so a mount
 * needs to pass nothing else; these prove each one does.
 */

vi.mock('$lib/ws', () => ({ on: () => () => {} }));

const HOUR = 3_600_000;
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let head: Record<string, unknown> | null = null;

/** A task its agent scheduled until `hours` from the real now. */
function scheduled<T extends OperatorNode>(base: T, hours: number): T {
	base.display = { label_key: 'waiting', tone_key: 'waiting' };
	base.scheduled_until = new Date(Date.now() + hours * HOUR).toISOString();
	base.execution = {
		...base.execution,
		condition: 'waiting',
		causes: [{
			kind: 'waiting', reason_key: 'watch', reason: 'a watch', owner_kind: 'thread', owner_id: 'agent',
			ends_at: new Date(Date.now() + hours * HOUR).toISOString(), scheduled_reason: 'Read the numbers',
		}],
	};
	return base;
}
const op = (id: string, hours: number, over: Partial<OperatorNode> = {}) =>
	scheduled(operatorNode({ id, title: id, ...over }), hours);

async function settle() {
	await tick();
	flushSync();
	for (let i = 0; i < 3; i += 1) {
		await new Promise((resolve) => setTimeout(resolve, 0));
		flushSync();
	}
}

function respond(body: unknown) {
	return {
		ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body),
		headers: { get: (key: string) => (key === 'ETag' ? '"r1"' : null) },
	};
}

function threadEnvelope(nodes: OperatorNode[], open: number, state = 'active'): OperatorEnvelope {
	return {
		contract_version: 2, as_of: 'now', target: { kind: 'thread', id: 't1', label: 'Thread' },
		revision_token: 'rev', attention: { awaiting_count: 0 },
		plans: [{
			plan: { id: 'plan-t1', owner_kind: 'thread', owner_id: 't1', state, revision: 1, updated_at: 'now' },
			owner: { label: 'Thread', project_id: null, thread_id: 't1' },
			progress: { done: 0, total: open, open, liveness: 'waiting', distribution: { ...EMPTY_DISTRIBUTION } },
			display: planDisplay('waiting', 'waiting'), oversize: null, current_node_ids: [], next_node_ids: [], nodes,
		}],
	} as OperatorEnvelope;
}

const titles = () => [...host.querySelectorAll('.tree-row')].map((row) =>
	row.classList.contains('scheduled-row') ? `{${row.querySelector('.row-title')?.textContent}}` : row.querySelector('.row-title')?.textContent ?? '?');

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	resetPlanRuntimeForTests();
	resetObserversForTests();
	token.set('test-bearer');
	connectionState.set('connected');
	activeThreadId.set(null);
	uiScope.set(null);
});
afterEach(() => {
	if (head) unmount(head);
	head = null;
	if (component) unmount(component);
	component = null;
	host.remove();
	resetObserversForTests();
	_resetProjectsForTests();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('the thread pane', () => {
	function mountPane() {
		const cap = document.createElement('div');
		host.appendChild(cap);
		head = mount(PlanHeadControls, { target: cap, props: { threadId: 't1' } });
		component = mount(ThreadPlanPane, { target: host, props: { threadId: 't1' } });
	}

	it('gathers scheduled work into the group', async () => {
		const nodes = [operatorNode({ id: 'open', title: 'Open work', state: 'active' }), op('A', 48), op('B', 72)];
		vi.stubGlobal('fetch', vi.fn(async () => respond(threadEnvelope(nodes, 3))));
		mountPane();
		await settle();
		expect(titles()).toEqual(['Open work', '{Scheduled}']);
	});

	it('never says "Nothing open" while the only open work is scheduled, and still counts it', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => respond(threadEnvelope([op('A', 48), op('B', 72)], 2))));
		mountPane();
		await settle();
		expect(host.textContent).not.toContain('Nothing open');
		expect(titles()).toEqual(['{Scheduled}']);
		expect(host.querySelector('.progress')?.textContent).toBe('2 left');
	});

	it('shows ordinary paused rows, not a group, while the plan is paused', async () => {
		const paused = ['A', 'B'].map((id) => {
			const row = op(id, 48);
			row.execution = { ...row.execution, condition: 'paused', causes: [] };
			row.scheduled_until = null;
			return row;
		});
		vi.stubGlobal('fetch', vi.fn(async () => respond(threadEnvelope(paused, 2, 'paused'))));
		mountPane();
		await settle();
		expect(titles()).toEqual(['A', 'B']);
	});
});

describe('the drill view', () => {
	it('gathers the focused subtree\'s scheduled children', () => {
		const nodes = [
			operatorNode({ id: 'root', title: 'Root', kind: 'phase', depth: 2 }),
			op('A', 48, { parent_id: 'root', depth: 3 }),
			op('B', 72, { parent_id: 'root', depth: 3 }),
		];
		component = mount(PlanDrillView, { target: host, props: { nodes, rootId: 'root', onBack: () => {} } });
		flushSync();
		expect(titles()).toContain('{Scheduled}');
		expect(titles()).not.toContain('A');
	});
});

describe('a nested subagent plan', () => {
	it('gathers inside the nested tree, one level under its step', () => {
		const step = treeNode({ id: 'step', title: 'Delegated step', state: 'active', child: CHILD_AVAILABLE });
		const childNodes = [scheduled(treeNode({ id: 'c1', title: 'C1' }), 48), scheduled(treeNode({ id: 'c2', title: 'C2' }), 72)];
		component = mount(PlanTree, {
			target: host,
			props: {
				nodes: [step],
				isChildExpanded: () => true,
				childRowState: () => ({ status: 'ready', planId: 'child-plan', nodes: childNodes, currentNodeIds: [], nextNodeIds: [], errorMessage: null }),
			},
		});
		flushSync();
		expect(titles()).toEqual(['Delegated step', '{Scheduled}']);
		expect(host.querySelector('.child-tree .scheduled-row')?.getAttribute('data-depth')).toBe('1');
	});
});

describe('the Activity plan row', () => {
	it('gathers scheduled work in an expanded plan', async () => {
		const index = {
			plan: { id: 'p-a', owner_kind: 'thread', owner_id: 'thread-a', state: 'active', revision: 1, updated_at: '2026-08-14T12:00:00Z' },
			owner: { label: 'Plan for thread-a', project_id: null, thread_id: 'thread-a' },
			progress: { open: 3, liveness: 'advancing' }, has_nodes: true,
			display: planDisplay('working', 'active'), priority_at: '2026-08-14T12:00:00Z',
		};
		const list: ActivityIndexEnvelope = {
			contract_version: 1, representation: 'activity_scope_index', as_of: '2026-08-14T12:00:00Z',
			target: { kind: 'workspace_scope', id: 'workspace', label: 'Workspace' }, revision_token: 'rev',
			plans: [index], attention: { awaiting_count: 0 },
		} as ActivityIndexEnvelope;
		const nodes = [operatorNode({ id: 'open', title: 'Open work', state: 'active' }), op('A', 48), op('B', 72)];
		const detail = threadEnvelope(nodes, 3);
		detail.target = { kind: 'thread', id: 'thread-a', label: 'Plan for thread-a' };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => respond(String(url).includes('/operator/threads/') ? detail : list)));
		threads.set([{ id: 'thread-a', user_id: 'u', profile: 'personal', title: 'A', model: null, project_id: null, created_at: 'x', updated_at: 'x' }]);
		component = mount(ActivityView, { target: host, props: { projectId: null, scopeName: 'Workspace', onSelectThread: vi.fn() } });
		await settle();
		(host.querySelector('.row-summary[aria-label="Expand Plan for thread-a"]') as HTMLButtonElement).click();
		await settle();
		expect(titles()).toEqual(['Open work', '{Scheduled}']);
	});
});
