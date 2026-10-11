import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { foldAllCompleted, foldLabel } from '$lib/components/plans/planFoldLevels';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

/**
 * Journey step 1: at every level of the tree, work the server marks `folded`
 * (finished more than a day ago) gathers into one "N completed" row at the
 * bottom of that level, so the level's top row is its current or next item.
 * The row opens in pages of five, newest first; progress is untouched.
 */

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

async function settle() {
	await tick();
	flushSync();
}

function hoursAgo(hours: number): string {
	return new Date(Date.UTC(2026, 8, 18, 12) - hours * 3_600_000).toISOString();
}

function done(id: string, over: Partial<OperatorNode> = {}): OperatorNode {
	return node({ id, title: id, state: 'accepted', folded: true, last_transition_at: hoursAgo(30), ...over });
}

/** Each rendered row as its title, and each fold row as `[level label]`. */
function rendered(): string[] {
	return [...host.querySelectorAll('.tree-row')].map((row) =>
		row.classList.contains('fold-row')
			? `[${row.querySelector('.fold-label')?.textContent?.trim()}]`
			: row.querySelector('.row-title')?.textContent ?? '?',
	);
}

async function press(label: string): Promise<void> {
	const button = [...host.querySelectorAll('button')].find((item) => item.textContent?.trim() === label);
	expect(button, `button "${label}"`).toBeTruthy();
	button!.click();
	await settle();
}

function mixedTree(): OperatorNode[] {
	return [
		done('Old A'),
		// Declared before the open rows, so a correct render still sinks it below
		// them rather than keeping this plan-order position (the defect this
		// journey follow-up fixes).
		node({ id: 'fresh', title: 'Just finished', state: 'accepted', last_transition_at: hoursAgo(1) }),
		node({ id: 'open', title: 'Open task' }),
		done('Old B'),
		node({ id: 'phase', title: 'Phase', state: 'active', branch_progress: { done: 3, total: 4 } }),
		done('Old child 1', { parent_id: 'phase', depth: 1 }),
		node({ id: 'live', title: 'Live child', state: 'active', parent_id: 'phase', depth: 1 }),
		done('Old child 2', { parent_id: 'phase', depth: 1 }),
	];
}

describe('PlanTree — finished work folds at the bottom of each level', () => {
	it('puts one fold row at the bottom of every level and leads each level with open work', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: mixedTree(), currentNodeIds: ['live'] }) });
		await settle();

		// 'fresh' is declared before 'open' and 'phase' in mixedTree(), so this
		// order proves it sinks to the bottom of its own level rather than
		// keeping its plan-order position: the top row is always open work.
		expect(rendered()).toEqual([
			'Open task',
			'Phase',
			'Live child',
			'[2 completed]',
			'Just finished',
			'[2 completed]',
		]);
	});

	it('orders several finished-but-not-yet-folded siblings newest settled first', async () => {
		const nodes = [
			node({ id: 'older', title: 'Finished 3h ago', state: 'accepted', last_transition_at: hoursAgo(3) }),
			node({ id: 'open', title: 'Open task' }),
			node({ id: 'newer', title: 'Finished 1h ago', state: 'accepted', last_transition_at: hoursAgo(1) }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['Open task', 'Finished 1h ago', 'Finished 3h ago']);
	});

	it('keeps a finished parent with an open descendant in the open group, not the finished group', async () => {
		const nodes = [
			node({ id: 'phase', title: 'Finished phase, open inside', state: 'accepted' }),
			node({ id: 'child', title: 'Open child', parent_id: 'phase', depth: 1 }),
			node({ id: 'open-sibling', title: 'Open sibling' }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes, currentNodeIds: ['child'] }) });
		await settle();
		// 'phase' itself is terminal, but its child is still open, so it must
		// not be treated as finished for ordering (mirrors the server's rule
		// that a finished parent never folds over open work).
		expect(rendered()).toEqual(['Finished phase, open inside', 'Open child', 'Open sibling']);
	});

	it('offers one fold control per level; there is no separate plan-wide toggle', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: mixedTree(), currentNodeIds: ['live'] }) });
		await settle();
		const labels = [...host.querySelectorAll('button')].map((button) => button.textContent?.trim());
		expect(labels).not.toContain('Hide completed');
		expect(labels.some((label) => label?.includes('completed'))).toBe(false);
		expect(rendered()).toContain('Just finished');
	});

	it('keeps progress from the server: folded work still counts as done', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: mixedTree(), currentNodeIds: ['live'] }) });
		await settle();
		const phase = [...host.querySelectorAll('.tree-row')].find(
			(row) => row.querySelector('.row-title')?.textContent === 'Phase',
		);
		expect(phase?.querySelector('.branch-progress')?.textContent).toContain('3/4');
	});

	it('opens a fold five at a time, newest first, and collapses back', async () => {
		const nodes = [
			node({ id: 'open', title: 'Open task' }),
			...Array.from({ length: 12 }, (_, i) => done(`Done ${i}`, { last_transition_at: hoursAgo(30 + i) })),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['Open task', '[12 completed]']);

		await press('Show 5');
		expect(rendered()).toEqual([
			'Open task', 'Done 0', 'Done 1', 'Done 2', 'Done 3', 'Done 4', '[12 completed]',
		]);
		await press('Show 5 more');
		expect(rendered()).toHaveLength(12);
		await press('Show 2 more');
		expect(rendered()).toHaveLength(14);
		expect([...host.querySelectorAll('button')].some((b) => b.textContent?.includes('more'))).toBe(false);

		await press('Collapse');
		expect(rendered()).toEqual(['Open task', '[12 completed]']);
	});

	it('keeps an expanded fold label whole instead of giving it truncation chrome', async () => {
		const nodes = [
			node({ id: 'open', title: 'Open task' }),
			...Array.from({ length: 19 }, (_, index) => done(`Done ${index}`)),
			done('Failed', { state: 'failed' }),
			...Array.from({ length: 5 }, (_, index) => done(`Closed ${index}`, { state: 'closed' })),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		await press('Show 5');

		const label = host.querySelector<HTMLElement>('.fold-row .fold-label');
		expect(label?.textContent?.trim()).toBe('19 completed, 1 failed, 5 closed');
		expect(label?.className).not.toMatch(/truncate|ellipsis/);
		expect(label?.style.textOverflow).toBe('');
	});

	it('shows no fold row inside a collapsed branch', async () => {
		const nodes = [
			node({ id: 'open', title: 'Open task' }),
			node({ id: 'phase', title: 'Phase', state: 'pending' }),
			node({ id: 'todo', title: 'Todo child', parent_id: 'phase', depth: 1 }),
			done('Old child', { parent_id: 'phase', depth: 1 }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['Open task', 'Phase']);
		const toggle = host.querySelector<HTMLButtonElement>('.branch-toggle');
		toggle!.click();
		await settle();
		expect(rendered()).toEqual(['Open task', 'Phase', 'Todo child', '[1 completed]']);
	});

	it('gives a parent whose only children are folded a disclosure that reaches them', async () => {
		const nodes = [
			node({ id: 'phase', title: 'Phase', state: 'pending' }),
			done('Old child', { parent_id: 'phase', depth: 1 }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		const toggle = host.querySelector<HTMLButtonElement>('.branch-toggle');
		expect(toggle, 'the parent offers disclosure').toBeTruthy();
		if (toggle?.getAttribute('aria-expanded') === 'false') {
			toggle.click();
			await settle();
		}
		expect(rendered()).toEqual(['Phase', '[1 completed]']);
	});

	it('names folded outcomes truthfully', () => {
		expect(foldLabel({ done: 2, failed: 1, closed: 0 })).toBe('2 completed, 1 failed');
		expect(foldLabel({ done: 0, failed: 0, closed: 1 })).toBe('1 closed');
	});

	it('wears the done mark only when every folded step completed', () => {
		expect(foldAllCompleted({ done: 2, failed: 0, closed: 0 })).toBe(true);
		expect(foldAllCompleted({ done: 1, failed: 1, closed: 0 })).toBe(false);
		expect(foldAllCompleted({ done: 0, failed: 0, closed: 1 })).toBe(false);
	});
});
