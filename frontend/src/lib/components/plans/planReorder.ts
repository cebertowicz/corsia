import type { OperatorNode } from '$lib/api/plans';

/** Position for the server's open-sibling list after moving beside a target. */
export function positionBeside(siblings: OperatorNode[], sourceId: string, targetId: string, before: boolean): number | null {
	const from = siblings.findIndex((node) => node.id === sourceId);
	const target = siblings.findIndex((node) => node.id === targetId);
	if (from < 0 || target < 0 || from === target) return null;
	return target - (from < target ? 1 : 0) + (before ? 0 : 1);
}
