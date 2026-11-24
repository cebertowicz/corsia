import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

/**
 * A workflow run's steps are a sequence: they show in the run's order (plan order, as the server sends
 * them), each row with its own state, whatever that state is. Nested runs too. Every other level keeps
 * its sort: open work, then waits soonest first, then finished work newest first.
 */

const NOW = new Date(2026, 10, 24, 12, 0);
const MIN = 60_000;

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

function rendered(): string[] {
	return [...host.querySelectorAll('.tree-row')].map((row) =>
		row.classList.contains('fold-row')
			? `[${row.querySelector('.fold-label')?.textContent?.trim()}]`
			: row.querySelector('.row-title')?.textContent ?? '?',
	);
}

const at = (minutesAgo: number) => new Date(NOW.getTime() - minutesAgo * MIN).toISOString();

/** One row under `parent`; `step` marks it a step of a workflow run. */
function row(id: string, parent: string | null, depth: number, state: string, step: string | null, over: Partial<OperatorNode> = {}): OperatorNode {
	return node({ id, title: id, parent_id: parent, depth, state, workflow_step: step, last_transition_at: at(0), ...over });
}

/** A row waiting on a watch that ends `minutes` from now. */
function waiting(id: string, parent: string | null, depth: number, step: string | null, minutes: number): OperatorNode {
	const base = row(id, parent, depth, 'active', step);
	base.display = { label_key: 'waiting', tone_key: 'waiting' };
	base.execution = {
		...base.execution,
		condition: 'waiting',
		causes: [{ kind: 'waiting', reason_key: 'watch', reason: 'a watch', owner_kind: 'thread', owner_id: 'agent', ends_at: new Date(NOW.getTime() + minutes * MIN).toISOString() }],
	};
	return base;
}

describe('a workflow run shows its steps in order', () => {
	it('keeps a general-work run 1..5 in plan order whatever each step is doing', async () => {
		const nodes = [
			row('Run', null, 0, 'active', null, { kind: 'phase', container: true }),
			row('1 Understand the ask', 'Run', 1, 'accepted', 'understand', { last_transition_at: at(50) }),
			row('2 Plan it', 'Run', 1, 'accepted', 'plan', { last_transition_at: at(30) }),
			row('3 Do it', 'Run', 1, 'active', 'do'),
			waiting('4 Fresh eyes', 'Run', 1, 'review', 20),
			row('5 Hand over', 'Run', 1, 'pending', 'hand_over'),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes, currentNodeIds: ['3 Do it'] }) });
		await settle();
		expect(rendered()).toEqual(['Run', '1 Understand the ask', '2 Plan it', '3 Do it', '4 Fresh eyes', '5 Hand over']);
	});

	it('keeps a nested review run in its own order under its step', async () => {
		const nodes = [
			row('Run', null, 0, 'active', null, { kind: 'phase', container: true }),
			row('1 Understand the ask', 'Run', 1, 'accepted', 'understand'),
			row('2 Review', 'Run', 1, 'active', 'review', { kind: 'phase', container: true }),
			row('2.1 Read it cold', '2 Review', 2, 'accepted', 'read', { last_transition_at: at(40) }),
			waiting('2.2 Check it', '2 Review', 2, 'check', 15),
			row('2.3 Verdict', '2 Review', 2, 'active', 'verdict'),
			row('3 Hand over', 'Run', 1, 'pending', 'hand_over'),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes, currentNodeIds: ['2.3 Verdict'] }) });
		await settle();
		expect(rendered()).toEqual(['Run', '1 Understand the ask', '2 Review', '2.1 Read it cold', '2.2 Check it', '2.3 Verdict', '3 Hand over']);
	});

	it('leaves a level that is not a run in its existing sort', async () => {
		const nodes = [
			row('Project', null, 0, 'active', null, { kind: 'phase', container: true }),
			row('Older finished', 'Project', 1, 'accepted', null, { last_transition_at: at(50) }),
			waiting('Waits a while', 'Project', 1, null, 20),
			row('Newer finished', 'Project', 1, 'accepted', null, { last_transition_at: at(5) }),
			row('Working', 'Project', 1, 'active', null),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes, currentNodeIds: ['Working'] }) });
		await settle();
		expect(rendered()).toEqual(['Project', 'Working', 'Waits a while', 'Newer finished', 'Older finished']);
	});
});
