import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get, type Writable } from 'svelte/store';
import type { OperatorNode } from '$lib/api/plans';
import { planFoldFields } from './fixtures/planFolds';

let mount: typeof import('svelte').mount;
let tick: typeof import('svelte').tick;
let flushSync: typeof import('svelte').flushSync;
let unmount: typeof import('svelte').unmount;
let PlanDrawerControl: typeof import('$lib/components/plans/PlanDrawerControl.svelte').default;
let activeThreadId: Writable<string | null>;
let token: Writable<string | null>;
let SidebarAttentionList: typeof import('$lib/components/SidebarAttentionList.svelte').default;
let threadPlanStates: typeof import('$lib/stores/threadPlanStates').threadPlanStates;
let resetPlanRuntimeForTests: typeof import('$lib/stores/plans.svelte').resetPlanRuntimeForTests;
let resetObserversForTests: typeof import('$lib/stores/planQuery.svelte').resetObserversForTests;
let operatorNode: (over?: Partial<OperatorNode>) => OperatorNode;
let planDisplay: typeof import('./fixtures/operatorPlan').planDisplay;
let EMPTY_DISTRIBUTION: typeof import('./fixtures/operatorPlan').EMPTY_DISTRIBUTION;
let planLeafReturn: typeof import('$lib/planLeafReturn');
let threadWorkspaces: typeof import('$lib/stores/artifactWorkspace').threadWorkspaces;
let host: HTMLElement;
let components: Record<string, unknown>[] = [];

// Transform the component graph once at collection, which has no timeout. vi.resetModules()
// below only re-evaluates cached transforms; the cold transform took ~5 s of the first
// beforeEach under full-suite load and could pass the 10 s hook timeout on a busy host.
await Promise.all([
	import('$lib/components/plans/PlanDrawerControl.svelte'),
	import('$lib/components/SidebarAttentionList.svelte'),
]);

function envelope(nodes = [operatorNode({ id: 'node-1', title: 'Choose a model', state: 'active' })]) {
	return {
		contract_version: 2,
		as_of: 'now',
		target: { kind: 'thread', id: 't1', label: 'Thread' },
		revision_token: 'r1',
		attention: { awaiting_count: 0 },
		plans: [{
			plan: { id: 'p1', owner_kind: 'thread', owner_id: 't1', state: 'active', revision: 1, updated_at: 'now' },
			owner: { label: 'Thread', project_id: null, thread_id: 't1' },
			progress: { done: 0, total: 1, open: 1, liveness: 'advancing', distribution: EMPTY_DISTRIBUTION },
			display: planDisplay('working', 'active'), oversize: null,
			current_node_ids: ['node-1'], next_node_ids: [],
			nodes, ...planFoldFields(nodes),
		}],
	};
}

async function settle() {
	await tick();
	flushSync();
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

beforeEach(async () => {
	vi.resetModules();
	Object.defineProperty(window, 'matchMedia', {
		configurable: true,
		value: () => ({ matches: true, addEventListener: () => {}, removeEventListener: () => {} }),
	});
	({ mount, tick, flushSync, unmount } = await import('svelte'));
	({ activeThreadId } = await import('$lib/stores/threads'));
	({ token } = await import('$lib/stores/auth'));
	({ threadPlanStates } = await import('$lib/stores/threadPlanStates'));
	SidebarAttentionList = (await import('$lib/components/SidebarAttentionList.svelte')).default;
	({ resetPlanRuntimeForTests } = await import('$lib/stores/plans.svelte'));
	({ resetObserversForTests } = await import('$lib/stores/planQuery.svelte'));
	({ operatorNode, planDisplay, EMPTY_DISTRIBUTION } = await import('./fixtures/operatorPlan'));
	PlanDrawerControl = (await import('$lib/components/plans/PlanDrawerControl.svelte')).default;
	planLeafReturn = await import('$lib/planLeafReturn');
	({ threadWorkspaces } = await import('$lib/stores/artifactWorkspace'));
	host = document.createElement('div');
	document.body.appendChild(host);
	resetPlanRuntimeForTests();
	resetObserversForTests();
	activeThreadId.set('t1');
	token.set('test-bearer');
	threadPlanStates.set({
		t1: { state: 'needs_input', label_key: null, paused: false, attention_items: [{ kind: 'decision', title: 'Choose a model', node_id: 'node-1' }] },
	});
});

function serve(body: ReturnType<typeof envelope>) {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
		ok: true, status: 200, json: async () => body, headers: { get: () => '"r1"' },
	}));
}

/** The phone chat's plan chip, plus the sidebar's attention list opened on thread t1. */
async function renderPhone(onSelect: (threadId: string) => Promise<void> = async () => {}) {
	components.push(mount(PlanDrawerControl, { target: host, props: { threadId: 't1' } }));
	components.push(mount(SidebarAttentionList, {
		target: host, props: { attention: { threadId: 't1', title: 'Needs you' }, onSelect },
	}));
	await settle();
}

afterEach(() => {
	for (const component of components.splice(0)) unmount(component);
	threadPlanStates.set({});
	host.remove();
	resetObserversForTests();
	vi.unstubAllGlobals();
});

describe('PlanDrawerControl attention-item click', () => {
	it('opens the phone plan drawer and focuses the item’s row', async () => {
		serve(envelope());
		await renderPhone();
		expect(document.querySelector('[data-adaptive-drawer]')).toBeNull();

		document.querySelector<HTMLButtonElement>('.attention-item')!.click();
		await settle();
		await settle();

		expect(document.querySelector('[data-adaptive-drawer]')).toBeTruthy();
		expect((document.activeElement as HTMLElement | null)?.closest('.tree-row')?.id).toBe('plan-row-node-1');
	});

	it('heads the drawer with Pause, the title and its count, and Done', async () => {
		serve(envelope());
		await renderPhone();
		document.querySelector<HTMLButtonElement>('.attention-item')!.click();
		await settle();
		await settle();

		const head = document.querySelector('[data-adaptive-drawer] .drawer-head')!;
		const buttons = [...head.querySelectorAll('button')].map((button) => button.textContent?.trim());
		expect(buttons).toEqual(['Pause', 'Done']);
		expect(head.querySelector('h2')?.textContent).toBe('Plan 1 left');
		expect(head.querySelector('.drawer-head-meta')?.textContent).toBe('1 left');
		// The pane under the header repeats neither the count nor Pause.
		const body = document.querySelector('[data-adaptive-drawer] .drawer-plan')!;
		expect(body.textContent).not.toContain('1 left');
		expect([...body.querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Pause')).toBe(false);
	});

	it('on a cold thread, opens the drawer at once and focuses the row once the plan arrives after the click', async () => {
		let answer!: () => void;
		vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => {
			answer = () => resolve({ ok: true, status: 200, json: async () => envelope(), headers: { get: () => '"r1"' } });
		})));
		activeThreadId.set('t0');
		await renderPhone(async (threadId) => activeThreadId.set(threadId));
		expect(host.querySelector('.plan-progress-chip')).toBeNull();

		document.querySelector<HTMLButtonElement>('.attention-item')!.click();
		await settle();
		expect(document.querySelector('[data-adaptive-drawer]')).toBeTruthy();
		expect(document.querySelector('[data-adaptive-drawer]')?.textContent).toContain('Loading plan');

		answer();
		await settle();
		await settle();

		expect((document.activeElement as HTMLElement | null)?.closest('.tree-row')?.id).toBe('plan-row-node-1');
	});

	it('returns from a leaf opened on a nested subagent-plan row to its owning step in a fresh drawer', async () => {
		const child = (planId: string, threadId: string) => ({
			available: true as const, plan_id: planId, thread_id: threadId,
			progress: { done: 0, total: 1, open: 1, liveness: 'advancing' as const },
		});
		const threadPlan = envelope([operatorNode({ id: 'spawn', title: 'Spawned', state: 'active', child: child('p-child', 'ct') })]);
		const childPlan = envelope([operatorNode({ id: 'sub', title: 'Nested step', child: child('p-grand', 'gt') })]);
		vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({
			ok: true, status: 200, json: async () => (url.includes('/nodes/') ? childPlan : threadPlan), headers: { get: () => '"r1"' },
		})));
		components.push(mount(PlanDrawerControl, {
			target: host,
			props: {
				threadId: 't1',
				onOpenChild: (nodeId: string, childThreadId: string, title: string, stepId?: string) =>
					planLeafReturn.openPlanSubagentLeaf('t1', nodeId, childThreadId, title, stepId),
			},
		}));
		await settle();
		host.querySelector<HTMLButtonElement>('.plan-progress-chip')!.click();
		await settle();
		document.querySelector<HTMLButtonElement>('[data-adaptive-drawer] .spawn-chip')!.click();
		await settle();
		await settle();

		document.querySelector<HTMLButtonElement>('[data-plan-child-open="sub"]')!.click();
		await settle();
		expect(document.querySelector('[data-adaptive-drawer]')).toBeNull();
		const workspace = get(threadWorkspaces).t1;
		const tab = workspace.tabs.find((candidate) => candidate.instanceId === workspace.activeTabId)!;
		expect(tab).toMatchObject({ sourceNodeId: 'sub', sourceStepId: 'spawn' });

		await planLeafReturn.returnToPlanNode(tab);
		await settle();
		await settle();

		const drawer = document.querySelector('[data-adaptive-drawer]');
		expect(drawer).toBeTruthy();
		expect(drawer?.querySelector('[data-plan-child-open="sub"]')).toBeNull();
		expect((document.activeElement as HTMLElement | null)?.closest('.tree-row')?.id).toBe('plan-row-spawn');
		expect(drawer?.textContent).not.toContain('no longer in this plan');
	});

	it('opens a person step’s instructions document directly from Sidebar Needs you', async () => {
		const instructions = {
			id: 'instructions-1', role: 'input' as const, integration: 'workspace', type: 'document',
			external_id: 'how-to.md', title: 'How to set up', path: '/how-to.md', state: 'active' as const,
			attention_state: null,
		};
		serve(envelope([operatorNode({
			id: 'person-step', title: 'Set up workspace', assignee: 'user', artifacts: [instructions],
		})]));
		threadPlanStates.set({
			t1: { state: 'needs_input', label_key: null, paused: false, attention_items: [
				{ kind: 'user_step', title: 'Set up workspace', node_id: 'person-step' },
			] },
		});
		await renderPhone();

		document.querySelector<HTMLButtonElement>('.attention-item')!.click();
		await settle();
		await settle();

		const workspace = get(threadWorkspaces).t1;
		const tab = workspace.tabs.find((candidate) => candidate.instanceId === workspace.activeTabId)!;
		expect(tab).toMatchObject({ artifactId: 'instructions-1', path: '/how-to.md', title: 'How to set up' });
	});

	it('says so in the drawer when the loaded plan no longer has the node', async () => {
		serve(envelope([]));
		await renderPhone();

		document.querySelector<HTMLButtonElement>('.attention-item')!.click();
		await settle();
		await settle();

		expect(document.querySelector('[data-adaptive-drawer]')?.textContent).toContain('That item is no longer in this plan');
	});
});
