import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import ThreadPlanPane from '$lib/components/plans/ThreadPlanPane.svelte';
import Pane from '$lib/components/Pane.svelte';
import { paneOpen } from '$lib/stores/paneOpen';
import { paneCollapsed } from '$lib/stores/paneCollapsed';
import { focusPlanNode, registerPlanSurface } from '$lib/planNodeFocus';
import { returnToPlanNode } from '$lib/planLeafReturn';
import { resetPlanRuntimeForTests } from '$lib/stores/plans.svelte';
import { resetObserversForTests } from '$lib/stores/planQuery.svelte';
import { activeThreadId } from '$lib/stores/threads';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import type { OperatorEnvelope, OperatorNode } from '$lib/api/plans';
import { EMPTY_DISTRIBUTION, operatorNode, planDisplay } from './fixtures/operatorPlan';
import { planFoldFields } from './fixtures/planFolds';

vi.mock('$lib/ws', () => ({ on: () => () => {} }));

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let unregisterSurface: () => void = () => {};

const childLink = (planId: string, threadId: string) => ({
	available: true as const, plan_id: planId, thread_id: threadId,
	progress: { done: 0, total: 1, open: 1, liveness: 'advancing' as const },
});

/** A root-level current step, a collapsed off-path branch (and one older failure the read does not list, see `OLD`), a subagent step, and a chain eight levels deep. */
function planNodes(threadId: string): OperatorNode[] {
	const chain = Array.from({ length: 8 }, (_, depth) => operatorNode({
		id: `d${depth}`, title: `Deep ${depth}`, depth, parent_id: depth > 0 ? `d${depth - 1}` : null,
	}));
	return [
		operatorNode({ id: 'now', title: `Now for ${threadId}`, state: 'active' }),
		operatorNode({ id: 'branch', title: 'Branch' }),
		operatorNode({ id: 'leaf', title: 'Leaf', depth: 1, parent_id: 'branch' }),
		// Malformed: past the overview depth with no parent to drill from.
		operatorNode({ id: 'orphan', title: 'Orphan', depth: 9 }),
		operatorNode({ id: 'spawn', title: 'Spawned', child: childLink('plan-child', 'child-thread') }),
		...chain,
	];
}

/** An older finished row the pane read does not list: the server counts it and serves it from its history. */
const OLD = {
	id: 'old', parent_id: null, kind: 'task', title: 'Old failure', state: 'failed', folded_at: '2026-09-01T00:00:00Z',
	fold_bucket: 'failed', display: { label_key: 'finished', tone_key: 'failed' },
	status_reason: null, folds: { done: 0, failed: 0, closed: 0 }, has_folded_children: false,
};

function envelope(threadId: string, nodes = planNodes(threadId)): OperatorEnvelope {
	const fields = planFoldFields(nodes);
	const withOld = threadId === 'child-thread'
		? fields
		: { folds: [{ parent_id: null, done: 0, failed: 1, closed: 0, more: 1 }], root_count: fields.root_count + 1 };
	return {
		contract_version: 2,
		as_of: 'now',
		target: { kind: 'thread', id: threadId, label: 'Thread' },
		revision_token: `r-${threadId}`,
		attention: { awaiting_count: 0 },
		plans: [{
			plan: { id: `plan-${threadId}`, owner_kind: 'thread', owner_id: threadId, state: 'active', revision: 1, updated_at: 'now' },
			owner: { label: 'Thread', project_id: null, thread_id: threadId },
			progress: { done: 0, total: 13, open: 12, liveness: 'advancing', distribution: { ...EMPTY_DISTRIBUTION, active: 1 } },
			display: planDisplay('working', 'active'),
			oversize: null,
			current_node_ids: ['now'],
			next_node_ids: [],
			nodes, ...withOld,
		}],
	};
}

/** The subagent's own plan: one step that itself opens a subagent leaf. */
const childEnvelope = () => envelope('child-thread', [
	operatorNode({ id: 'sub', title: 'Nested step', child: childLink('plan-grandchild', 'grandchild-thread') }),
]);

function ok(body: OperatorEnvelope, tag: string) {
	return { ok: true, status: 200, json: async () => body, headers: { get: () => `"${tag}"` } };
}

/** Serve each thread's plan (and the subagent step's plan); a thread listed in `hanging` never answers. */
function serve(hanging: string[] = []) {
	vi.stubGlobal('fetch', vi.fn((url: string) => {
		const plain = (body: unknown) => Promise.resolve({ ok: true, status: 200, json: async () => body, headers: { get: () => null } });
		if (url.includes('/folded?')) return plain({ plan_id: 'plan-t1', parent: 'root', items: [OLD], next_cursor: null });
		if (url.endsWith('/nodes/old/path')) return plain({ plan_id: 'plan-t1', node: OLD, ancestors: [], page: null });
		if (url.includes('/nodes/')) return Promise.resolve(ok(childEnvelope(), 'child'));
		const threadId = url.split('/').pop() ?? '';
		if (hanging.includes(threadId)) return new Promise(() => {});
		return Promise.resolve(ok(envelope(threadId), threadId));
	}));
}

/** Hold every plan read until the test answers it: `answer(true)` serves the plan, `answer(false)` fails with a 500. */
function serveHeld() {
	const held: ((served: boolean) => void)[] = [];
	vi.stubGlobal('fetch', vi.fn((url: string) => new Promise((resolve) => {
		const threadId = url.split('/').pop() ?? '';
		held.push((served) => resolve(served
			? ok(envelope(threadId), threadId)
			: { ok: false, status: 500, json: async () => ({}), headers: { get: () => null } }));
	})));
	return { answer: (served: boolean) => held.splice(0).forEach((respond) => respond(served)) };
}

async function settle() {
	for (let i = 0; i < 3; i += 1) {
		await tick();
		flushSync();
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
	flushSync();
}

function focusedRowId(): string | null {
	return (document.activeElement as HTMLElement | null)?.closest('.tree-row')?.id ?? null;
}

function row(nodeId: string): HTMLElement | undefined {
	return [...host.querySelectorAll<HTMLElement>('.tree-row')].find((element) => element.id === `plan-row-${nodeId}`);
}

type OpenChild = (nodeId: string, threadId: string, title: string, stepId?: string) => void;

async function mountPane(props: { threadId: string; onOpenChild?: OpenChild }) {
	component = mount(ThreadPlanPane, { target: host, props });
	await settle();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	resetPlanRuntimeForTests();
	resetObserversForTests();
	token.set('test-bearer');
	connectionState.set('connected');
	activeThreadId.set('t1');
	unregisterSurface = registerPlanSurface(() => true);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	unregisterSurface();
	host.remove();
	resetObserversForTests();
	vi.unstubAllGlobals();
});

describe('ThreadPlanPane attention-node focus', () => {
	it('reveals a node under a collapsed branch and focuses its row', async () => {
		serve();
		await mountPane({ threadId: 't1' });
		expect(row('leaf')).toBeUndefined();

		await focusPlanNode('t1', 'leaf');
		await settle();

		expect(row('leaf')).toBeTruthy();
		expect(focusedRowId()).toBe('plan-row-leaf');
		expect(host.querySelector('.drill-view')).toBeNull();
	});

	it('pages an older finished node in from the server\'s history and focuses its row', async () => {
		serve();
		await mountPane({ threadId: 't1' });
		expect(row('old')).toBeUndefined();

		await focusPlanNode('t1', 'old');
		await settle();

		expect(focusedRowId()).toBe('plan-row-old');
	});

	it('shows a node past the overview depth through the drill view, leaving the overview depth alone', async () => {
		serve();
		await mountPane({ threadId: 't1' });

		await focusPlanNode('t1', 'd7');
		await settle();

		expect(host.querySelector('.drill-title')?.textContent).toBe('Deep 6');
		expect(focusedRowId()).toBe('plan-row-d7');
	});

	it('does not refocus or re-expand when the overview remounts after drill and Back', async () => {
		serve();
		await mountPane({ threadId: 't1' });
		await focusPlanNode('t1', 'd7');
		await settle();

		host.querySelector<HTMLButtonElement>('.drill-back')!.click();
		await settle();

		expect(host.querySelector('.drill-view')).toBeNull();
		expect(focusedRowId()).toBeNull();
		expect(row('d1')).toBeUndefined();
		expect(row('d7')).toBeUndefined();
	});

	it('retires a handled node: reopening the thread focuses and expands nothing', async () => {
		serve();
		await mountPane({ threadId: 't1' });
		await focusPlanNode('t1', 'leaf');
		await settle();
		expect(focusedRowId()).toBe('plan-row-leaf');

		unmount(component!);
		(document.activeElement as HTMLElement | null)?.blur();
		await mountPane({ threadId: 't1' });

		expect(row('leaf')).toBeUndefined();
		expect(focusedRowId()).toBeNull();
	});

	it('says so when the plan no longer has the node', async () => {
		serve();
		await mountPane({ threadId: 't1' });

		await focusPlanNode('t1', 'gone');
		await settle();

		expect(host.textContent).toContain('That item is no longer in this plan');
		expect(focusedRowId()).toBeNull();
	});

	it('says so when the node cannot be shown, instead of focusing nothing silently', async () => {
		serve();
		await mountPane({ threadId: 't1' });

		await focusPlanNode('t1', 'orphan');
		await settle();

		expect(host.textContent).toContain('That item couldn’t be shown in this plan');
		expect(focusedRowId()).toBeNull();
	});

	it('drops a node still waiting for its plan when the thread changes', async () => {
		serve(['t1']);
		const props = $state({ threadId: 't1' });
		await mountPane(props);
		await focusPlanNode('t1', 'leaf');
		await settle();

		activeThreadId.set('t2');
		props.threadId = 't2';
		await settle();

		expect(host.textContent).toContain('Now for t2');
		expect(row('leaf')).toBeUndefined();
		expect(focusedRowId()).toBeNull();
		expect(host.textContent).not.toContain('no longer in this plan');
	});

	/** Expand the subagent step, then open the nested step's own subagent leaf; returns the tab source it opened with. */
	async function openNestedLeaf() {
		const opened = vi.fn<OpenChild>();
		await mountPane({ threadId: 't1', onOpenChild: opened });
		row('spawn')!.querySelector<HTMLButtonElement>('.spawn-chip')!.click();
		await settle();
		host.querySelector<HTMLButtonElement>('[data-plan-child-open="sub"]')!.click();
		const [sourceNodeId, , , sourceStepId] = opened.mock.calls[0];
		return { sourceNodeId, sourceStepId };
	}

	it('returns to a step of an expanded subagent plan by its rendered row, without a false notice', async () => {
		serve();
		const source = await openNestedLeaf();
		expect(source).toEqual({ sourceNodeId: 'sub', sourceStepId: 'spawn' });

		await returnToPlanNode(source);
		await settle();

		expect(focusedRowId()).toBe('plan-row-plan-child-thread:sub');
		expect(host.textContent).not.toContain('no longer in this plan');
	});

	it('returns to the owning step when the pane remounted between opening and return, without a false notice', async () => {
		serve();
		const source = await openNestedLeaf();
		unmount(component!);
		(document.activeElement as HTMLElement | null)?.blur();
		await mountPane({ threadId: 't1' });
		expect(host.querySelector('[data-plan-child-open="sub"]')).toBeNull();

		await returnToPlanNode(source);
		await settle();

		expect(focusedRowId()).toBe('plan-row-spawn');
		expect(host.textContent).not.toContain('no longer in this plan');
	});

	it('says so when the nested node’s owning step is gone too', async () => {
		serve();
		await mountPane({ threadId: 't1' });

		await returnToPlanNode({ sourceNodeId: 'sub', sourceStepId: 'gone' });
		await settle();

		expect(host.textContent).toContain('That item is no longer in this plan');
		expect(focusedRowId()).toBeNull();
	});

	it('focuses from the last known plan when the refresh it waited for fails, and a later fresh read does not refocus', async () => {
		serve();
		await mountPane({ threadId: 't1' });
		unmount(component!);
		const reads = serveHeld();
		await mountPane({ threadId: 't1' });
		await focusPlanNode('t1', 'leaf');
		await settle();
		expect(focusedRowId()).toBeNull();

		reads.answer(false);
		await settle();
		expect(host.textContent).toContain('Showing last known data.');
		expect(focusedRowId()).toBe('plan-row-leaf');

		(document.activeElement as HTMLElement).blur();
		host.querySelector<HTMLButtonElement>('.status-banner button')!.click();
		await settle();
		reads.answer(true);
		await settle();
		expect(host.textContent).not.toContain('Showing last known data.');
		expect(focusedRowId()).toBeNull();
	});

	it('drops the node with a notice when the plan fails to load, and a later successful load does not take focus', async () => {
		const reads = serveHeld();
		await mountPane({ threadId: 't1' });
		await focusPlanNode('t1', 'leaf');
		await settle();

		reads.answer(false);
		await settle();
		expect(host.textContent).toContain('The plan couldn’t load, so that item can’t be shown');
		expect(focusedRowId()).toBeNull();

		[...host.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent?.includes('Retry'))!.click();
		await settle();
		reads.answer(true);
		await settle();
		expect(host.textContent).toContain('Now for t1');
		expect(host.textContent).not.toContain('so that item can’t be shown');
		expect(row('leaf')).toBeUndefined();
		expect(focusedRowId()).toBeNull();
	});

	it('stores nothing when no pane is mounted, so a later mount focuses nothing', async () => {
		serve();
		await focusPlanNode('t1', 'leaf');
		await settle();

		await mountPane({ threadId: 't1' });

		expect(row('leaf')).toBeUndefined();
		expect(focusedRowId()).toBeNull();
	});
});

describe('desktop Pane attention-node focus', () => {
	it('opens a closed pane and its collapsed Plan section, then focuses the row', async () => {
		unregisterSurface();
		serve();
		paneOpen.set(false);
		paneCollapsed.set(true);
		component = mount(Pane, {
			target: host,
			props: { sections: [{ title: 'Plan', kind: 'plan', initialOpen: false }] },
		});
		await settle();
		expect(host.querySelector('.thread-plan-pane')).toBeNull();

		await focusPlanNode('t1', 'leaf');
		await settle();

		expect(get(paneOpen)).toBe(true);
		expect(get(paneCollapsed)).toBe(false);
		expect(focusedRowId()).toBe('plan-row-leaf');
	});

	it('returns from a subagent leaf to its node through the same path, revealing a collapsed branch', async () => {
		unregisterSurface();
		serve();
		paneCollapsed.set(true);
		component = mount(Pane, { target: host, props: { sections: [{ title: 'Plan', kind: 'plan' }] } });
		await settle();

		await returnToPlanNode({ sourceNodeId: 'leaf' });
		await settle();

		expect(get(paneCollapsed)).toBe(false);
		expect(focusedRowId()).toBe('plan-row-leaf');
	});

	it('drops the request when the pane has no Plan section', async () => {
		unregisterSurface();
		serve();
		paneOpen.set(false);
		component = mount(Pane, { target: host, props: { sections: [{ title: 'Roadmap', empty: 'No roadmap yet' }] } });
		await settle();

		await focusPlanNode('t1', 'leaf');
		await settle();

		expect(get(paneOpen)).toBe(false);
		expect(focusedRowId()).toBeNull();
	});
});
