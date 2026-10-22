import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { indexPlanNodes } from '$lib/components/plans/planTreeFilter';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

const NOW = new Date(2026, 9, 20, 8, 0).getTime();
const HOUR = 3_600_000;

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(NOW);
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.useRealTimers();
});

async function settle() {
	await tick();
	flushSync();
}

function waiting(id: string, hours: number): OperatorNode {
	return node({
		id, title: id, parent_id: 'group', depth: 1,
		display: { label_key: 'waiting', tone_key: 'waiting' },
		scheduled_until: new Date(NOW + hours * HOUR).toISOString(),
		execution: {
			condition: 'waiting', primary_reason: null,
			causes: [{ kind: 'waiting', reason_key: 'watch', reason: 'a watch', owner_kind: 'thread', owner_id: 'agent', ends_at: new Date(NOW + hours * HOUR).toISOString() }],
			selection: null, conflict: null, attempt_count: 0, friction_count: 0, attempts: [],
		},
	});
}

function tree(child: OperatorNode): OperatorNode[] {
	return [
		node({ id: 'group', title: 'Group', kind: 'phase', container: true, state: 'active' }),
		child,
		node({ id: 'done', title: 'Done', parent_id: 'group', depth: 1, state: 'accepted', folded: true }),
	];
}

function groupToggle(): HTMLButtonElement {
	const toggle = host.querySelector<HTMLButtonElement>('.branch-toggle[aria-label^="Expand Group"], .branch-toggle[aria-label^="Collapse Group"]');
	expect(toggle).toBeTruthy();
	return toggle!;
}

describe('idle plan-group disclosure', () => {
	it('does not default-open a group for a current leaf gathered into Scheduled', () => {
		const nodes = tree(waiting('scheduled', 48));
		const input: Parameters<typeof indexPlanNodes>[1] & { now: number } = {
			maxDepth: 6, currentNodeIds: ['scheduled'], now: NOW,
		};
		const index = indexPlanNodes(nodes, input);
		expect(index.defaultExpandedBranches.has('group')).toBe(false);
	});

	it('keeps scheduled and finished rows hidden below the initially collapsed group until the user opens it', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: tree(waiting('scheduled', 48)), currentNodeIds: ['scheduled'] }) });
		await settle();
		expect(groupToggle().getAttribute('aria-expanded')).toBe('false');
		expect(host.querySelector('.scheduled-row')).toBeNull();
		expect(host.querySelector('.fold-row')).toBeNull();
		groupToggle().click();
		await settle();
		expect(groupToggle().getAttribute('aria-expanded')).toBe('true');
		expect(host.querySelector('.scheduled-row')).toBeTruthy();
		expect(host.querySelector('.fold-row')).toBeTruthy();
	});

	it('opens the group when the minute clock brings a current scheduled wait within a day', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: tree(waiting('scheduled', 25)), currentNodeIds: ['scheduled'] }) });
		await settle();
		expect(groupToggle().getAttribute('aria-expanded')).toBe('false');
		vi.advanceTimersByTime(2 * HOUR);
		await settle();
		expect(groupToggle().getAttribute('aria-expanded')).toBe('true');
	});

	it.each([
		['running work', node({ id: 'running', title: 'running', parent_id: 'group', depth: 1, state: 'active' }), ['running'], true],
		['blocked attention', node({ id: 'blocked', title: 'blocked', parent_id: 'group', depth: 1, attention_path: ['blocked'] }), [], true],
		['review raised', node({ id: 'review', title: 'review', parent_id: 'group', depth: 1, state: 'review_requested' }), ['review'], true],
		['imminent timed wait', waiting('soon', 12), ['soon'], true],
		['later scheduled wait', waiting('later', 48), ['later'], false],
		['paused work', node({ id: 'paused', title: 'paused', parent_id: 'group', depth: 1, execution: { condition: 'paused', primary_reason: null, causes: [], selection: null, conflict: null, attempt_count: 0, friction_count: 0, attempts: [] } }), [], false],
	] as const)('%s opens the group', async (_state, child, currentNodeIds, expanded) => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: tree(child), currentNodeIds: [...currentNodeIds] }) });
		await settle();
		expect(groupToggle().getAttribute('aria-expanded')).toBe(String(expanded));
	});

	it('keeps a finished-only group collapsed', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: tree(node({ id: 'finished', title: 'finished', parent_id: 'group', depth: 1, state: 'accepted', folded: true })) }) });
		await settle();
		expect(groupToggle().getAttribute('aria-expanded')).toBe('false');
	});

	// Svelte 5 destroys a keyed-each batch only when every removed item has an out transition, so a
	// level's Scheduled and fold rows (which had none) kept the whole batch on screen after a close.
	describe('closing an opened group with real motion', () => {
		const paused = () => node({
			id: 'paused', title: 'paused', parent_id: 'group', depth: 1,
			execution: { condition: 'paused', primary_reason: null, causes: [], selection: null, conflict: null, attempt_count: 0, friction_count: 0, attempts: [] },
		});
		const animate = Element.prototype.animate;

		beforeEach(() => {
			document.documentElement.style.setProperty('--dur-tight', '100ms');
			document.documentElement.style.setProperty('--dur-standard', '200ms');
			Element.prototype.animate = function (_keyframes: Keyframe[] | PropertyIndexedKeyframes | null, options?: number | KeyframeAnimationOptions) {
				const run = { onfinish: null as null | (() => void), playState: 'running', effect: null as unknown, cancel() {}, finished: new Promise<void>(() => {}) };
				const duration = typeof options === 'number' ? options : Number(options?.duration ?? 0);
				setTimeout(() => { run.playState = 'finished'; run.onfinish?.(); }, duration);
				return run as unknown as Animation;
			};
		});
		afterEach(() => {
			document.documentElement.style.removeProperty('--dur-tight');
			document.documentElement.style.removeProperty('--dur-standard');
			Element.prototype.animate = animate;
		});

		it('removes the group\'s Scheduled, fold and paused rows together once the exit motion ends', async () => {
			const nodes = [
				node({ id: 'group', title: 'Group', kind: 'phase', container: true, state: 'active' }),
				paused(), waiting('scheduled', 48),
				node({ id: 'done', title: 'Done', parent_id: 'group', depth: 1, state: 'accepted', folded: true }),
			];
			component = mount(PlanTree, { target: host, props: withFolds({ nodes, currentNodeIds: ['scheduled'] }) });
			await settle();
			groupToggle().click();
			await settle();
			vi.advanceTimersByTime(1000);
			await settle();
			expect(host.querySelector('.scheduled-row')).toBeTruthy();
			expect(host.querySelector('.fold-row')).toBeTruthy();
			expect(host.querySelector('#plan-row-paused, [data-flip-key$="paused"]')).toBeTruthy();

			groupToggle().click();
			await settle();
			vi.advanceTimersByTime(1000);
			await settle();
			expect(groupToggle().getAttribute('aria-expanded')).toBe('false');
			expect(host.querySelector('.scheduled-row')).toBeNull();
			expect(host.querySelector('.fold-row')).toBeNull();
			expect(host.querySelector('[data-flip-key$="paused"]')).toBeNull();
		});
	});
});
