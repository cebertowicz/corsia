import type { OperatorNode } from '$lib/api/plans';

interface FocusableTree {
	focusRow(nodeId: string): boolean;
}

interface PlanTreeRowsOptions {
	scope: () => string;
	visibleNodes: () => OperatorNode[];
	isBranchExpanded(nodeId: string): boolean;
	element: () => HTMLElement | null;
	childTrees: () => Record<string, FocusableTree | null>;
}

/** DOM ownership and focus helpers for a tree whose rows remain flat siblings. */
export function createPlanTreeRows({ scope, visibleNodes, isBranchExpanded, element, childTrees }: PlanTreeRowsOptions) {
	function rowId(nodeId: string): string {
		return `plan-row-${scope()}${nodeId}`;
	}

	function controlledRowIds(nodeId: string): string | undefined {
		if (!isBranchExpanded(nodeId)) return undefined;
		const ids = visibleNodes()
			.filter((child) => child.parent_id === nodeId)
			.map((child) => rowId(child.id));
		return ids.length > 0 ? ids.join(' ') : undefined;
	}

	function focusRow(nodeId: string): boolean {
		const body = element()?.querySelector<HTMLButtonElement>(`[id="${rowId(nodeId)}"] > .row-body`);
		body?.focus();
		return !!body || Object.values(childTrees()).some((tree) => tree?.focusRow(nodeId));
	}

	function detailsId(nodeId: string): string {
		return `plan-node-details-${scope()}${nodeId}`;
	}

	return { rowId, controlledRowIds, focusRow, detailsId };
}
