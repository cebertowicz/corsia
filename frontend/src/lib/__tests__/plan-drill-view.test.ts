import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanDrillView from '$lib/components/plans/PlanDrillView.svelte';
import { resetPlanRuntimeForTests } from '$lib/stores/plans.svelte';
import { resetObserversForTests } from '$lib/stores/planQuery.svelte';
import { token } from '$lib/stores/auth';
import type { OperatorNode } from '$lib/api/plans';
import { EMPTY_DISTRIBUTION, operatorNode, planDisplay } from './fixtures/operatorPlan';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	resetPlanRuntimeForTests();
	resetObserversForTests();
	token.set('test-bearer');
	vi.unstubAllGlobals();
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	resetObserversForTests();
	vi.restoreAllMocks();
});

async function settle() {
	await tick();
	flushSync();
	await new Promise((r) => setTimeout(r, 0));
	flushSync();
}

function node(over: Partial<OperatorNode>): OperatorNode {
	return operatorNode(over);
}

describe('PlanDrillView', () => {
	const nodes = [
		node({ id: 'root', title: 'Root', depth: 2 }),
		node({ id: 'child-a', title: 'Descendant A', parent_id: 'root', depth: 3 }),
		node({ id: 'unrelated', title: 'Different branch', depth: 2 }),
	];

	it('shows only the focused subtree, normalizing depth, and hides unrelated siblings', () => {
		component = mount(PlanDrillView, { target: host, props: { nodes, rootId: 'root', onBack: () => {} } });
		flushSync();
		expect(host.textContent).toContain('Descendant A');
		expect(host.textContent).not.toContain('Different branch');
	});

	it('calls onBack when the Back control is used', () => {
		const onBack = vi.fn();
		component = mount(PlanDrillView, { target: host, props: { nodes, rootId: 'root', onBack } });
		flushSync();
		(host.querySelector('.drill-back') as HTMLButtonElement).click();
		expect(onBack).toHaveBeenCalledOnce();
	});

	it('uses the same PlanTree renderer (row markup) as the scope card', () => {
		component = mount(PlanDrillView, { target: host, props: { nodes, rootId: 'root', onBack: () => {} } });
		flushSync();
		expect(host.querySelector('.plan-tree')).toBeTruthy();
		expect(host.querySelectorAll('.tree-row').length).toBeGreaterThan(0);
	});

	it('B4: preserves canonical server depth-first order — does not breadth-first reorder', () => {
		// Canonical server order (depth-first): R, A, A1, A2, B, B1, B2
		const dfsNodes: OperatorNode[] = [
			node({ id: 'R', title: 'R', depth: 0 }),
			node({ id: 'A', title: 'A', parent_id: 'R', depth: 1 }),
			node({ id: 'A1', title: 'A1', parent_id: 'A', depth: 2 }),
			node({ id: 'A2', title: 'A2', parent_id: 'A', depth: 2 }),
			node({ id: 'B', title: 'B', parent_id: 'R', depth: 1 }),
			node({ id: 'B1', title: 'B1', parent_id: 'B', depth: 2 }),
			node({ id: 'B2', title: 'B2', parent_id: 'B', depth: 2 }),
		];
		component = mount(PlanDrillView, { target: host, props: { nodes: dfsNodes, rootId: 'R', onBack: () => {} } });
		flushSync();
		const titles = [...host.querySelectorAll('.row-title')].map((el) => el.textContent ?? '');
		expect(titles).toEqual(['R', 'A', 'A1', 'A2', 'B', 'B1', 'B2']);
	});

	it('R6: isDescendantOfRoot terminates when a queried node sits in a cycle that never reaches root', () => {
		// x <-> y cycle, disconnected from root: walking from x must not loop forever.
		const cyclic = [
			node({ id: 'root', title: 'Root', depth: 0 }),
			node({ id: 'x', title: 'X', parent_id: 'y', depth: 1 }),
			node({ id: 'y', title: 'Y', parent_id: 'x', depth: 1 }),
		];
		const start = Date.now();
		component = mount(PlanDrillView, { target: host, props: { nodes: cyclic, rootId: 'root', onBack: () => {} } });
		flushSync();
		expect(Date.now() - start).toBeLessThan(1000);
		// Neither x nor y is a descendant of root — the cycle must not hang or false-include them.
		expect(host.textContent).not.toContain('X');
		expect(host.textContent).not.toContain('Y');
	});

	it('M3: the subagent chip inside the drill view is a functional control, not a dead one', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: true, status: 200,
			json: () => Promise.resolve({
				contract_version: 2, as_of: 'now', target: { kind: 'thread', id: 'child', label: 'Child' },
				revision_token: 'r', attention: { awaiting_count: 0 },
				plans: [{
					plan: { id: 'child-plan', owner_kind: 'thread', owner_id: 'child', state: 'active', revision: 1, updated_at: 'now' },
					owner: { label: 'Child', project_id: null, thread_id: 'child' },
					progress: { done: 1, total: 2, open: 1, liveness: 'advancing', distribution: { ...EMPTY_DISTRIBUTION, active: 1 } },
					display: planDisplay('working', 'active'),
					current_node_ids: [], next_node_ids: [],
					nodes: [node({ id: 'child-step', title: 'Child step' })],
				}],
			}),
			headers: { get: () => '"r"' },
		}));
		const withChild: OperatorNode[] = [
			node({ id: 'root', title: 'Root', depth: 0 }),
			node({
				id: 'delegated', title: 'Delegated', parent_id: 'root', depth: 1,
				child: { available: true, plan_id: 'p2', thread_id: 'child-thread', progress: { done: 1, total: 2, open: 1, liveness: 'advancing' } },
			}),
		];
		component = mount(PlanDrillView, { target: host, props: { nodes: withChild, rootId: 'root', onBack: () => {} } });
		flushSync();
		const chip = host.querySelector('.spawn-chip') as HTMLButtonElement;
		expect(chip.getAttribute('aria-expanded')).toBe('false');
		chip.click();
		await settle();
		expect(chip.getAttribute('aria-expanded')).toBe('true');
		await settle();
		expect(host.textContent).toContain('Child step');
		expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/plans/operator/nodes/delegated/child');
	});
});
