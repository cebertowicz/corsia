import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import {
	INDENT_RESERVE, INDENT_STEP, MIN_TITLE, indentOffset, indentStep, indentVar,
} from '$lib/components/plans/planIndent';
import nodeDetailsSource from '$lib/components/plans/PlanNodeDetails.svelte?raw';
import historyDetailsSource from '$lib/components/plans/PlanHistoryDetails.svelte?raw';
import foldHistorySource from '$lib/components/plans/PlanFoldHistory.svelte?raw';
import treeSource from '$lib/components/plans/PlanTree.svelte?raw';
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

/** The tree widths measured in the render: the 1440 desktop plan column and the 393 phone drawer. */
const WIDTHS = [283, 337];
/**
 * What a delegated row ("N left" chip, chevron, status mark, gaps) spends beside its indent and
 * title, measured on the ellipsized 'Hand off ...' rows of the render (`_checks.txt`, furnitureEllipsized
 * plus the row's 4px left and 8px right padding): the widest row there is.
 */
const DELEGATED_OVERHEAD: Record<number, number> = { 283: 110 + 12, 337: 127.3 + 12 };

describe('plan indent: one source for the per-level step', () => {
	it('steps one full step per level while the title has room', () => {
		expect(indentOffset(0, 345)).toBe(0);
		expect(indentOffset(1, 345)).toBe(INDENT_STEP);
		expect(indentOffset(3, 345)).toBe(3 * INDENT_STEP);
	});

	it('is linear without a measured width', () => {
		expect(indentOffset(12, 0)).toBe(12 * INDENT_STEP);
		expect(indentStep(5, 0)).toBe(INDENT_STEP);
	});

	it.each(WIDTHS)('keeps the title at least its minimum at depth 8 and 12 (%ipx tree)', (width) => {
		for (const depth of [8, 12, 30]) {
			expect(width - INDENT_RESERVE - indentOffset(depth, width)).toBeGreaterThanOrEqual(MIN_TITLE);
		}
	});

	it.each(WIDTHS)('never refuses a level: offsets keep growing, steps only shrink (%ipx tree)', (width) => {
		let previousStep = Infinity;
		for (let depth = 1; depth <= 12; depth += 1) {
			const step = indentOffset(depth, width) - indentOffset(depth - 1, width);
			expect(step).toBeGreaterThan(0);
			expect(step).toBeLessThanOrEqual(previousStep);
			previousStep = step;
		}
	});

	it('does not move depth 0 or 1 at either width', () => {
		for (const width of WIDTHS) {
			expect(indentOffset(0, width)).toBe(0);
			expect(indentOffset(1, width)).toBe(INDENT_STEP);
		}
		expect(indentOffset(2, 337)).toBe(2 * INDENT_STEP);
	});

	it.each(WIDTHS)('keeps a delegated row (chip and chevron) title at its minimum at depth 8 and 12 (%ipx tree)', (width) => {
		for (const depth of [8, 12]) {
			expect(width - DELEGATED_OVERHEAD[width] - indentOffset(depth, width)).toBeGreaterThanOrEqual(MIN_TITLE);
		}
	});

	it('reserves at least the widest measured row, with room for a two-digit count', () => {
		expect(INDENT_RESERVE).toBeGreaterThanOrEqual(Math.max(...Object.values(DELEGATED_OVERHEAD)) + 8);
	});

	it('is written as a CSS length the rows, details and history rows all read', () => {
		expect(indentVar(2, 345)).toBe(`${2 * INDENT_STEP}px`);
		for (const source of [nodeDetailsSource, historyDetailsSource, foldHistorySource, treeSource]) {
			expect(source).not.toMatch(/var\(--depth\)\s*\*\s*\d+px/);
		}
	});

	it('keeps details aligned with their title at desktop and phone reserves', () => {
		for (const [width, reserve] of [[283, 122], [337, 148]] as const) {
			const detailsMinusTitle = [0, 1, 4, 8].map((depth) => {
				const rowTitleLeft = 30 + Number.parseFloat(indentVar(depth, width, reserve));
				const detailsLeft = 34 + Number.parseFloat(indentVar(depth, width, reserve));
				return detailsLeft - rowTitleLeft;
			});
			expect(new Set(detailsMinusTitle).size, `${width}px tree`).toBe(1);
		}
		// PlanTree has one reserve-aware indent path; a direct call can silently fall back to 148px.
		expect(treeSource.match(/indentVar\(/g)).toHaveLength(1);
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

	it('steps details by the same step as rows on a phone', () => {
		// Phone details used a 12px step against the rows' 22px; the rule is now one custom property.
		expect(nodeDetailsSource).not.toMatch(/--depth\)\s*\*\s*12px/);
		expect(historyDetailsSource).not.toMatch(/--depth\)\s*\*\s*12px/);
		expect(nodeDetailsSource).toMatch(/var\(--indent\)/);
		expect(historyDetailsSource).toMatch(/var\(--indent\)/);
	});
});
