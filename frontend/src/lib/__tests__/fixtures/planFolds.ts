import type { OperatorFoldLevel, OperatorNode } from '$lib/api/plans';
import type { ChildRowState } from '$lib/components/plans/childExpansion.svelte';
import type { FoldedWork } from '$lib/components/plans/planFoldLevels';

/**
 * The folds the server would send for rows a test lists: every folded row, by the
 * parent it sits under and the tone it shows. Tests that list only part of a level
 * state the level's `more` and counts themselves instead.
 */
export function foldsOf(nodes: readonly OperatorNode[]): OperatorFoldLevel[] {
	const levels = new Map<string | null, OperatorFoldLevel>();
	for (const node of nodes) {
		if (!node.folded) continue;
		const parent = node.parent_id ?? null;
		const level = levels.get(parent) ?? { parent_id: parent, done: 0, failed: 0, closed: 0, more: 0 };
		const tone = node.display.tone_key;
		level[tone === 'complete' ? 'done' : tone === 'failed' ? 'failed' : 'closed'] += 1;
		levels.set(parent, level);
	}
	return [...levels.values()];
}

/** The two plan-entry fields the server derives from the plan's rows: its folds and its top-level root count. */
export function planFoldFields(nodes: readonly OperatorNode[]): { folds: OperatorFoldLevel[]; root_count: number } {
	return { folds: foldsOf(nodes), root_count: nodes.filter((node) => node.parent_id === null).length };
}

export function foldedFor(nodes: readonly OperatorNode[], planId = 'plan-1'): FoldedWork {
	return { planId, levels: foldsOf(nodes) };
}

/** Props whose `folded` follows `props.nodes` as a test changes them (`$state` props), unless the test sets `folded`. */
export function withLiveFolds<P extends { nodes: OperatorNode[]; folded?: FoldedWork | null }>(props: P): P {
	return new Proxy(props, {
		get: (target, key, receiver) =>
			key === 'folded' && target.folded === undefined ? foldedFor(target.nodes) : Reflect.get(target, key, receiver),
		has: (target, key) => key === 'folded' || Reflect.has(target, key),
	});
}

/** Mount props for a tree whose server folds are those of its listed rows, unless the test sets `folded`. */
export function withFolds<P extends { nodes: OperatorNode[]; folded?: FoldedWork | null }>(props: P): P {
	return props.folded === undefined ? { ...props, folded: foldedFor(props.nodes) } : props;
}

/** A child plan's row state as the expansion store builds it: the server's folds are those of the rows listed. */
export function childState(state: Omit<ChildRowState, 'folds' | 'rootCount'>): ChildRowState {
	return { ...state, ...{ folds: foldsOf(state.nodes), rootCount: state.nodes.filter((node) => node.parent_id === null).length } };
}
