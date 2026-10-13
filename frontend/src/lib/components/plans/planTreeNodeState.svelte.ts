import type { OperatorNode } from '$lib/api/plans';
import { isFinished } from './planTreeFold';

/** A terminal node stays visibly open while an ancestor is blocked. */
export function isVisibleFinished(node: OperatorNode, openBlocked: ReadonlySet<string>): boolean {
	return isFinished(node) && !openBlocked.has(node.id);
}
