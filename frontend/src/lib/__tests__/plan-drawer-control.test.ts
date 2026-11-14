import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get, type Writable } from 'svelte/store';
import type { OperatorNode, ThreadPlanStateEntry } from '$lib/api/plans';
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
let listedNeedsYou: typeof import('$lib/stores/threadPlanStates').listedNeedsYou;
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
	({ threadPlanStates, listedNeedsYou } = await import('$lib/stores/threadPlanStates'));
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
		// The chip also asks for this thread's row state, so every request held by this
		// stub lands together once the cold load resolves; the plan read stays the held one.
		let held: Array<() => void> = [];
		vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => {
			held.push(() => resolve({ ok: true, status: 200, json: async () => envelope(), headers: { get: () => '"r1"' } }));
		})));
		const answer = () => {
			const pending = held;
			held = [];
			for (const resolve of pending) resolve();
		};
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

		expect(document.querySelector('[data-adaptive-drawer]')?.textContent).toContain('That task isn’t in this plan.');
	});
});

/**
 * The chip goes orange while the thread owes the person by the left menu's own rule
 * (`isYourMove` on the store's row state), so pausing the item lifts the chip and
 * the menu's dot together.
 */
describe('PlanDrawerControl needs-you chip', () => {
	function rowState(state: ThreadPlanStateEntry['state']): ThreadPlanStateEntry {
		return {
			state,
			label_key: null,
			paused: state === 'paused',
			attention_items: state === 'needs_input' ? [{ kind: 'decision', title: 'Choose a model', node_id: 'node-1' }] : [],
		};
	}

	/** The phone chip alone, with the open thread t1's row state set to `state` (null: nothing read yet). */
	async function renderChip(state: ThreadPlanStateEntry['state'] | null): Promise<HTMLButtonElement> {
		serve(envelope());
		threadPlanStates.set(state === null ? {} : { t1: rowState(state) });
		components.push(mount(PlanDrawerControl, { target: host, props: { threadId: 't1' } }));
		await settle();
		const chip = host.querySelector<HTMLButtonElement>('.plan-progress-chip');
		expect(chip, 'the chip renders for a plan with open work').not.toBeNull();
		return chip!;
	}

	it.each(['needs_input', 'blocked_on_you'] as const)('marks the chip as the person’s move for %s', async (state) => {
		const chip = await renderChip(state);
		expect(chip.classList.contains('needs-you')).toBe(true);
		expect(chip.getAttribute('aria-label')).toBe('Open plan — 1 left · Needs you');
	});

	it.each(['paused', 'idle', 'moving', 'waiting', 'failed'] as const)('leaves the chip as it is for %s', async (state) => {
		const chip = await renderChip(state);
		expect(chip.classList.contains('needs-you')).toBe(false);
		expect(chip.getAttribute('aria-label')).toBe('Open plan — 1 left');
	});

	it('leaves the chip as it is with no row state read yet', async () => {
		const chip = await renderChip(null);
		expect(chip.classList.contains('needs-you')).toBe(false);
		expect(chip.getAttribute('aria-label')).toBe('Open plan — 1 left');
	});

	it('lifts the mark when the item is paused, without a reload', async () => {
		const chip = await renderChip('needs_input');
		expect(chip.classList.contains('needs-you')).toBe(true);

		threadPlanStates.set({ t1: rowState('paused') });
		await settle();
		expect(chip.classList.contains('needs-you')).toBe(false);
		expect(chip.getAttribute('aria-label')).toBe('Open plan — 1 left');
	});

	it('asks the store for the open thread’s row state without moving the menu’s dot', async () => {
		serve(envelope());
		threadPlanStates.set({ t1: rowState('needs_input') });
		components.push(mount(PlanDrawerControl, { target: host, props: { threadId: 't1' } }));
		await settle();

		const stateReads = vi.mocked(fetch).mock.calls.map(([input]) => String(input))
			.filter((url) => url.includes('/api/plans/operator/thread-states'));
		expect(
			stateReads.some((url) => url.includes('ids=t1')),
			`the chip’s thread is read like a menu row; reads seen: ${stateReads.join(' , ')}`,
		).toBe(true);
		// Reading it must not put it on the menu: the menu's dot is the listed set's own.
		expect(get(listedNeedsYou)).toBe(false);
	});
});

/**
 * jsdom applies no component CSS (`css` is off in the vitest config), so the colour
 * itself is guarded on the authored rule, the way `fab-elevation.test.ts` and
 * `design-tokens.test.ts` guard what only the stylesheet carries.
 */
describe('PlanDrawerControl needs-you colours', () => {
	const css = readFileSync(resolve(__dirname, '../components/plans/PlanDrawerControl.svelte'), 'utf8');

	/** The declarations of exactly one authored rule, comments removed. */
	function declarations(selector: string): string {
		const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
		const rule = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(css.replace(/\/\*[\s\S]*?\*\//g, ''));
		expect(rule, `no authored rule for ${selector}`).not.toBeNull();
		return rule![1];
	}

	it('paints the dot and the count with the menu’s needs-you tokens', () => {
		// One dot and halo rule, coloured through --mark-colour, so needs-you adds no raw shadow.
		const dot = declarations('.progress-mark');
		expect(dot).toContain('background: var(--mark-colour);');
		expect(dot).toContain('box-shadow: 0 0 0 4px color-mix(in srgb, var(--mark-colour) 13%, transparent);');
		expect(declarations('.plan-progress-chip.needs-you .progress-mark')).toContain('--mark-colour: var(--amber);');
		expect(declarations('.plan-progress-chip.needs-you b')).toContain('color: var(--amber-text);');
	});
});
