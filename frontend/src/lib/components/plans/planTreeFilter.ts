import type { OperatorNode } from '$lib/api/plans';
import { nodeStateTone } from './planDisplay';
import type { OperatorFoldLevel } from '$lib/api/plans';
import type { FoldedWork, FoldLevelView } from './planFoldLevels';
import {
	arrangeItems, openBlockedIds, revealedIds, ROOT_LEVEL, type PlanTreeItem,
} from './planTreeFold';

/**
 * The pure row-selection rules behind `PlanTree`.
 *
 * These live outside the component because "which rows are visible" and "which
 * parents may disclose something" are the two rules a disclosure defect hides
 * in, and they are worth reading and testing without mounting anything.
 */

export interface PlanTreeIndex {
	/** Every node by id, for ancestor walks. */
	byId: Map<string, OperatorNode>;
	/** Ancestors of current and attention-path nodes, expanded by default. */
	defaultExpandedBranches: Set<string>;
	/** Parents whose children sit past `maxDepth`, offering a drill affordance. */
	boundaryParents: Set<string>;
	/** Terminal nodes an open descendant keeps out of the finished group (`planTreeFold`). */
	openBlocked: Set<string>;
}

export interface PlanTreeViewInput {
	nodes: OperatorNode[];
	index: PlanTreeIndex;
	maxDepth: number;
	isBranchExpanded: (nodeId: string) => boolean;
	/** Each level that has folded work (`planFoldLevels`), and how many of its rows the user has paged open. */
	levels: Map<string, FoldLevelView>;
	revealed: Record<string, number>;
	/** The clock the Scheduled rows are read against, and which levels have theirs open (`planScheduled`). */
	now?: number;
	isScheduledOpen?: (level: string) => boolean;
}

export interface PlanTreeView {
	/** Rows to render: depth and fold filters, then disclosure filter, always both. */
	visibleNodes: OperatorNode[];
	/** What renders, in order: rows, each level closed by its fold row. */
	items: PlanTreeItem[];
	/** Parents that actually have something to disclose right now. */
	branchParents: Set<string>;
}

/**
 * A subagent plan nested under its delegated step, without a sole wrapper root.
 *
 * Workers often open their plan with one task restating the step they were
 * given, then put the real steps under it (#2212). Nested under that step, the
 * wrapper is a second row saying the same thing, so its children render
 * directly under the step instead. The step row already carries the child
 * plan's state, and a root with no children is the work itself and stays.
 * A wrapper with state of its own stays too, since no other row would show
 * it: its own delegated child plan, the current-step marker, or a failure or
 * ask for the user (debt 88).
 *
 * "Sole" is the server's root count (`root_count`, folded roots included),
 * not the roots this read listed: a plan with one open root and settled roots
 * keeps its wrapper. The wrapper's folded children, which the read may not
 * list, take over the top level and are paged from the wrapper (`rootParent`).
 */
export function withoutSoleWrapper(
	nodes: OperatorNode[], currentNodeIds: string[] = [], work: FoldedWork | null = null, rootCount?: number,
): { nodes: OperatorNode[]; work: FoldedWork | null } {
	const roots = nodes.filter((node) => !node.parent_id);
	const wrapper = roots[0];
	if (roots.length !== 1 || (rootCount ?? 1) !== 1) return { nodes, work };
	const folded = work?.levels.some((level) => level.parent_id === wrapper.id) ?? false;
	if (!nodes.some((node) => node.parent_id === wrapper.id) && !folded) return { nodes, work };
	if (hasOwnState(wrapper, currentNodeIds)) return { nodes, work };
	const levels = (work?.levels ?? [])
		.filter((level) => level.parent_id !== null)
		.map((level): OperatorFoldLevel => (level.parent_id === wrapper.id ? { ...level, parent_id: null } : level));
	return {
		nodes: nodes
			.filter((node) => node.id !== wrapper.id)
			.map((node) => ({
				...node,
				parent_id: node.parent_id === wrapper.id ? null : node.parent_id,
				depth: Math.max(0, node.depth - 1),
			})),
		work: work && { ...work, levels, rootParent: wrapper.id },
	};
}

/** The nodes and folds a subagent's plan hands its nested tree under its step: none until that plan has loaded. */
export function childPlanView(
	child: { status: string; nodes: OperatorNode[]; currentNodeIds: string[]; planId?: string | null; folds?: OperatorFoldLevel[]; rootCount?: number },
): { nodes: OperatorNode[]; work: FoldedWork | null } {
	if (child.status !== 'ready' && child.status !== 'stale') return { nodes: [], work: null };
	const work = { planId: child.planId ?? '', levels: child.folds ?? [] };
	return withoutSoleWrapper(child.nodes, child.currentNodeIds, work, child.rootCount);
}

const OWN_STATE_TONES = new Set(['failed']);

function hasOwnState(node: OperatorNode, currentNodeIds: string[]): boolean {
	return (
		node.child !== null
		|| currentNodeIds.includes(node.id)
		|| node.requires_user_action
		|| OWN_STATE_TONES.has(nodeStateTone(node))
	);
}

/** Terminates on a cyclic `parent_id`: a node is visited at most once (R6). */
export function ancestorChain(byId: Map<string, OperatorNode>, id: string): string[] {
	const chain: string[] = [];
	const seen = new Set<string>();
	let current = byId.get(id);
	while (current && !seen.has(current.id)) {
		seen.add(current.id);
		chain.push(current.id);
		current = current.parent_id ? byId.get(current.parent_id) : undefined;
	}
	return chain;
}


/** Index the flat node list once for every rule that needs structure. */
export function indexPlanNodes(
	nodes: OperatorNode[],
	{ maxDepth, currentNodeIds, deepFolded = [] }: { maxDepth: number; currentNodeIds: string[]; deepFolded?: Iterable<string> },
): PlanTreeIndex {
	const byId = new Map(nodes.map((node) => [node.id, node]));
	const defaultExpandedBranches = new Set<string>();
	const revealAncestors = (nodeId: string) => {
		const chain = ancestorChain(byId, nodeId);
		// Skip index 0: the node itself is revealed, not expanded.
		for (let index = 1; index < chain.length; index += 1) defaultExpandedBranches.add(chain[index]);
	};
	for (const id of currentNodeIds) revealAncestors(id);
	for (const node of nodes) for (const id of node.attention_path) revealAncestors(id);

	const boundaryParents = new Set<string>();
	for (const node of nodes) {
		if (node.depth === maxDepth + 1 && node.parent_id) boundaryParents.add(node.parent_id);
	}
	// Folded work past the rendered depth is not listed, but its parent still offers the drill.
	for (const parent of deepFolded) boundaryParents.add(parent);

	return { byId, defaultExpandedBranches, boundaryParents, openBlocked: openBlockedIds(byId) };
}

/**
 * Whether a node survives the current *mode* filter, independent of whether an
 * ancestor happens to be collapsed.
 *
 * One predicate so every consumer applies the same rule, and no plan state
 * removes a row outright: an off-path branch stays reachable, collapsed by the
 * disclosure filter until the user opens it (PLANS-AC19). The per-level fold
 * row is the one control that hides finished work; there is no second toggle.
 */
function passesModeFilter(node: OperatorNode, input: PlanTreeViewInput, revealed: Set<string>): boolean {
	if (node.depth > input.maxDepth) return false;
	if (node.folded && !revealed.has(node.id)) return false;
	return true;
}

/** A malformed cyclic parent chain terminates and never hides the row forever. */
function isHiddenByCollapsedAncestor(
	node: OperatorNode,
	input: PlanTreeViewInput,
	branchParents: Set<string>,
): boolean {
	const seen = new Set<string>();
	let parentId = node.parent_id;
	while (parentId && !seen.has(parentId)) {
		seen.add(parentId);
		if (branchParents.has(parentId) && !input.isBranchExpanded(parentId)) return true;
		parentId = input.index.byId.get(parentId)?.parent_id ?? null;
	}
	return false;
}

/** Resolve the rows to render and the parents allowed to offer disclosure. */
export function buildPlanTreeView(input: PlanTreeViewInput): PlanTreeView {
	const { nodes, index, levels } = input;
	const revealed = revealedIds(levels, input.revealed);
	const passing = nodes.filter((node) => passesModeFilter(node, input, revealed));

	// Derived from passing rows rather than all nodes, because a parent whose
	// children are all filtered away would otherwise render a chevron and flip
	// `aria-expanded` while revealing nothing. A level's fold row is something
	// to disclose too, whenever it holds any folded work.
	const branchParents = new Set<string>();
	for (const node of passing) {
		if (node.parent_id && index.byId.has(node.parent_id)) branchParents.add(node.parent_id);
	}
	for (const level of levels.keys()) if (level !== ROOT_LEVEL) branchParents.add(level);

	const visibleNodes = passing.filter(
		(node) => !isHiddenByCollapsedAncestor(node, input, branchParents),
	);
	const shown = new Set(visibleNodes.map((node) => node.id));
	const items = arrangeItems({
		visible: visibleNodes, byId: index.byId, levels, openBlocked: index.openBlocked,
		now: input.now, isScheduledOpen: input.isScheduledOpen,
		isLevelOpen: (level) => level === ROOT_LEVEL
			|| (shown.has(level) && branchParents.has(level) && input.isBranchExpanded(level)),
	});
	return { visibleNodes, items, branchParents };
}
