/**
 * Owns only the user's expansion intent for spawned child plans. Expanding
 * declares a child key through `planKeySet`; it never awaits, never decides
 * success, and never holds server state itself — `plans.svelte.ts` does.
 *
 * `getHostKey` names the currently-observed key whose payload embeds a
 * child's progress (a `thread:` key), so a child-owner nudge also refreshes
 * that host. Omit it where no such key is independently observed (e.g. a
 * scope card, where the workspace/project key already self-matches).
 */
import { planKeySet } from '$lib/stores/planQuery.svelte';
import type { OperatorKey, PlanKey } from '$lib/stores/planKeys';
import type { PlanLoadStatus } from '$lib/stores/plans.svelte';
import type { OperatorNode } from '$lib/api/plans';

/** A child plan as `PlanTree` renders it: the same tree, scoped by its plan id. */
export interface ChildRowState {
	status: PlanLoadStatus;
	planId: string | null;
	nodes: OperatorNode[];
	currentNodeIds: string[];
	nextNodeIds: string[];
	errorMessage: string | null;
}

/** A child plan nobody has asked for yet: what a tree with no expansion owner reads. */
export const EMPTY_CHILD_STATE: ChildRowState = {
	status: 'idle', planId: null, nodes: [], currentNodeIds: [], nextNodeIds: [], errorMessage: null,
};

export function createChildExpansion(getHostKey?: () => PlanKey | null) {
	let expandedIds = $state<string[]>([]);
	const keys = $derived(expandedIds.map((id): OperatorKey => ({ kind: 'child', id })));
	const set = planKeySet(() => keys, getHostKey);

	return {
		isExpanded: (nodeId: string) => expandedIds.includes(nodeId),
		toggle(nodeId: string) {
			expandedIds = expandedIds.includes(nodeId)
				? expandedIds.filter((id) => id !== nodeId)
				: [...expandedIds, nodeId];
		},
		state(nodeId: string): ChildRowState {
			const entry = set.state({ kind: 'child', id: nodeId });
			const plan = entry.payload?.plans[0];
			return {
				status: entry.status,
				planId: plan?.plan.id ?? null,
				nodes: plan?.nodes ?? [],
				currentNodeIds: plan?.current_node_ids ?? [],
				nextNodeIds: plan?.next_node_ids ?? [],
				errorMessage: entry.errorMessage,
			};
		},
		retry(nodeId: string) {
			set.retry({ kind: 'child', id: nodeId });
		},
	};
}
