import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import {
	INDENT_STEP, MIN_TITLE, indentOffset, indentStep, indentVar,
} from '$lib/components/plans/planIndent';
import { CHILD_AVAILABLE, node } from './fixtures/planTreeNode';
import { childState, withFolds } from './fixtures/planFolds';

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

/** The tree widths and reserves measured in the desktop column and phone drawer. */
const TREE_DIMENSIONS = [
	{ width: 283, reserve: 122 },
	{ width: 337, reserve: 148 },
] as const;

describe('plan indent: one source for the per-level step', () => {
	it('steps one full step per level while the title has room', () => {
		expect(indentOffset(0, 345, 122)).toBe(0);
		expect(indentOffset(1, 345, 122)).toBe(INDENT_STEP);
		expect(indentOffset(3, 345, 122)).toBe(3 * INDENT_STEP);
	});

	it('is linear without a measured width', () => {
		expect(indentOffset(12, 0, 122)).toBe(12 * INDENT_STEP);
		expect(indentStep(5, 0, 122)).toBe(INDENT_STEP);
	});

	it('keeps the title at least its minimum at both production reserves', () => {
		for (const { width, reserve } of TREE_DIMENSIONS) {
			for (const depth of [8, 12, 30]) {
				expect(width - reserve - indentOffset(depth, width, reserve)).toBeGreaterThanOrEqual(MIN_TITLE);
			}
		}
	});

	it('never refuses a level: offsets keep growing, steps only shrink', () => {
		for (const { width, reserve } of TREE_DIMENSIONS) {
			let previousStep = Infinity;
			for (let depth = 1; depth <= 12; depth += 1) {
				const step = indentOffset(depth, width, reserve) - indentOffset(depth - 1, width, reserve);
				expect(step).toBeGreaterThan(0);
				expect(step).toBeLessThanOrEqual(previousStep);
				previousStep = step;
			}
		}
	});

	it('does not move depth 0 or 1 at either production dimension', () => {
		for (const { width, reserve } of TREE_DIMENSIONS) {
			expect(indentOffset(0, width, reserve)).toBe(0);
			expect(indentOffset(1, width, reserve)).toBe(INDENT_STEP);
		}
		expect(indentOffset(2, 337, 148)).toBe(2 * INDENT_STEP);
	});

	it('is written as a CSS length', () => {
		expect(indentVar(2, 345, 122)).toBe(`${2 * INDENT_STEP}px`);
	});
});

describe('plan indent: details sit under their row', () => {
	const offsetOf = (el: Element | null) => (el as HTMLElement | null)?.style.getPropertyValue('--indent') ?? '';

	it('uses the row offset for details inside a nested worker plan', () => {
		const nodes = [node({ id: 'a', title: 'Parent', child: CHILD_AVAILABLE })];
		const wrapper = node({ id: 'w', title: 'Worker step', depth: 0 });
		const sibling = node({ id: 'w2', title: 'Worker second step', depth: 0 });
		const inner = node({ id: 's1', title: 'Worker substep', parent_id: 'w', depth: 1 });
		component = mount(PlanTree, {
			target: host,
			props: withFolds({
				nodes,
				isChildExpanded: (id: string) => id === 'a',
				childRowState: () => childState({
					status: 'ready', planId: 'child-plan', nodes: [wrapper, sibling, inner],
					currentNodeIds: ['s1'], nextNodeIds: [], errorMessage: null,
				}),
			}),
		});
		flushSync();
		const row = host.querySelector('#plan-row-child-plan\\:s1') as HTMLElement;
		expect(row).toBeTruthy();
		(row.querySelector('.row-body') as HTMLButtonElement).click();
		flushSync();
		const details = host.querySelector('#plan-node-details-child-plan\\:s1');
		expect(details).toBeTruthy();
		expect(offsetOf(row)).not.toBe('');
		expect(offsetOf(details)).toBe(offsetOf(row));
		// The nested plan's depth continues its parent's: one level for the parent row, one for the substep.
		expect(offsetOf(row)).toBe(`${2 * INDENT_STEP}px`);
	});

	it('uses the row offset for details at a plain depth', () => {
		const a = node({ id: 'a', title: 'Root step' });
		const b = node({ id: 'b', title: 'Child step', parent_id: 'a', depth: 1 });
		const c = node({ id: 'c', title: 'Grandchild step', parent_id: 'b', depth: 2 });
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: [a, b, c], expandBranchesByDefault: true }) });
		flushSync();
		const row = host.querySelector('#plan-row-c') as HTMLElement;
		(row.querySelector('.row-body') as HTMLButtonElement).click();
		flushSync();
		expect(offsetOf(host.querySelector('#plan-node-details-c'))).toBe(offsetOf(row));
		expect(offsetOf(row)).toBe(`${2 * INDENT_STEP}px`);
	});

});
