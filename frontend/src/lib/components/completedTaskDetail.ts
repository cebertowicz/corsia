import type { OperatorNode } from '$lib/api/plans';

/** What an expanded completed-task row shows from the thread's plan read. */
export interface TaskDetail {
	/** Ancestor titles, top first, ending at the parent; the task's own title is the row above. */
	crumbs: string[];
	/** An ancestor is missing from the read (archived or removed), so the path starts mid-tree. */
	gap: boolean;
	/** The description, else `Done when: <acceptance criteria>`; null when neither is written. */
	text: string | null;
}

export function indexNodes(nodes: OperatorNode[]): Map<string, OperatorNode> {
	return new Map(nodes.map((node) => [node.id, node]));
}

/** Null when the task is not in this read: another plan holds it. */
export function taskDetail(byId: Map<string, OperatorNode>, nodeId: string): TaskDetail | null {
	const node = byId.get(nodeId);
	if (!node) return null;
	const crumbs: string[] = [];
	const seen = new Set([node.id]);
	let gap = false;
	for (let parentId = node.parent_id; parentId; ) {
		const parent = byId.get(parentId);
		if (!parent || seen.has(parent.id)) { gap = !parent; break; }
		seen.add(parent.id);
		crumbs.unshift(parent.title);
		parentId = parent.parent_id;
	}
	const description = node.description?.trim();
	const criteria = node.acceptance_criteria?.trim();
	return { crumbs, gap, text: description || (criteria ? `Done when: ${criteria}` : null) };
}

/** The full path for the accessible name, e.g. `… › Top › Parent`. */
export function breadcrumbName(detail: TaskDetail): string {
	return [...(detail.gap ? ['…'] : []), ...detail.crumbs].join(' › ');
}
