import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';

/**
 * The Scheduled row's open state and focus while a tree stays mounted: a level's
 * flag is kept while its group is absent, a reveal request opens the group that
 * hides its row, and focus never falls to the page when the row holding it goes.
 */

const NOW = new Date(2026, 8, 25, 10, 0);
const HOUR = 3_600_000;
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

function waiting(id: string, hours: number): OperatorNode {
	const base = node({ id, title: id });
	base.display = { label_key: 'waiting', tone_key: 'waiting' };
	base.scheduled_until = new Date(NOW.getTime() + hours * HOUR).toISOString();
	base.execution = {
		...base.execution,
		condition: 'waiting',
		causes: [{
			kind: 'waiting', reason_key: 'watch', reason: 'a watch', owner_kind: 'thread', owner_id: 'agent',
			ends_at: new Date(NOW.getTime() + hours * HOUR).toISOString(), scheduled_reason: 'Revisit',
		}],
	};
	return base;
}

const titles = () => [...host.querySelectorAll('.tree-row')].map((row) =>
	row.classList.contains('scheduled-row') ? `{${row.querySelector('.row-title')?.textContent}}` : row.querySelector('.row-title')?.textContent ?? '?');
const groupToggle = () => host.querySelector<HTMLButtonElement>('.scheduled-row button')!;

describe('a level\'s open flag', () => {
	it('survives the group dropping to one row and re-forming, as the user left it', async () => {
		const props = $state({ nodes: [waiting('A', 48), waiting('B', 72)] });
		component = mount(PlanTree, { target: host, props });
		await settle();
		groupToggle().click();
		await settle();
		expect(titles()).toEqual(['{Scheduled}', 'A', 'B']);

		props.nodes = [waiting('A', 48)];
		await settle();
		expect(titles()).toEqual(['{Scheduled}', 'A']);

		props.nodes = [waiting('A', 48), waiting('B', 72), waiting('C', 90)];
		await settle();
		expect(titles()).toEqual(['{Scheduled}', 'A', 'B', 'C']);
	});

	it('re-forms closed when the user never opened it', async () => {
		const props = $state({ nodes: [waiting('A', 48), waiting('B', 72)] });
		component = mount(PlanTree, { target: host, props });
		await settle();
		props.nodes = [waiting('A', 48)];
		await settle();
		props.nodes = [waiting('A', 48), waiting('B', 72)];
		await settle();
		expect(titles()).toEqual(['{Scheduled}']);
	});

	it('starts closed on a fresh mount', async () => {
		const props = $state({ nodes: [waiting('A', 48), waiting('B', 72)] });
		component = mount(PlanTree, { target: host, props });
		await settle();
		groupToggle().click();
		await settle();
		unmount(component);
		component = mount(PlanTree, { target: host, props });
		await settle();
		expect(titles()).toEqual(['{Scheduled}']);
	});
});

describe('revealing a row the group hides', () => {
	it('opens the group so the row exists to focus', async () => {
		let tree: { revealNode: (id: string) => boolean; focusRow: (id: string) => boolean };
		tree = mount(PlanTree, { target: host, props: { nodes: [waiting('A', 48), waiting('B', 72)] } }) as typeof tree;
		component = tree as unknown as Record<string, unknown>;
		await settle();
		expect(titles()).toEqual(['{Scheduled}']);
		expect(tree.revealNode('B')).toBe(true);
		await settle();
		expect(titles()).toEqual(['{Scheduled}', 'A', 'B']);
		expect(tree.focusRow('B')).toBe(true);
		expect(document.activeElement?.closest('.tree-row')?.querySelector('.row-title')?.textContent).toBe('B');
	});
});

describe('focus when the row holding it goes', () => {
	it('keeps focus on the toggle when a member leaves and one remains', async () => {
		const soon = waiting('Soon', 24);
		soon.scheduled_until = new Date(NOW.getTime() + 24 * HOUR + MIN).toISOString();
		soon.execution.causes[0].ends_at = soon.scheduled_until;
		component = mount(PlanTree, { target: host, props: { nodes: [soon, waiting('Other', 72)] } });
		await settle();
		groupToggle().focus();
		expect(document.activeElement).toBe(groupToggle());
		vi.advanceTimersByTime(3 * MIN);
		await settle();
		expect(titles()).toEqual(['Soon', '{Scheduled}']);
		expect(document.activeElement).toBe(groupToggle());
	});

	it('keeps focus on the toggle while the group stays', async () => {
		component = mount(PlanTree, { target: host, props: { nodes: [waiting('A', 48), waiting('B', 72)] } });
		await settle();
		const toggle = groupToggle();
		toggle.focus();
		toggle.click();
		await settle();
		expect(document.activeElement).toBe(toggle);
	});
});
