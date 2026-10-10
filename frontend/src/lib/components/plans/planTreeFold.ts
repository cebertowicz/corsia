import type { OperatorNode } from '$lib/api/plans';
import { isScheduledMember, parentIdsOf, scheduledClusters } from './planScheduled';
import type { ScheduledView } from './planDisplay';
import { waitEndsAtMs } from './planWaits';

/**
 * Where folded work sits in `PlanTree`: the pure per-level rules.
 *
 * The server marks a node `folded` the moment its branch has nothing open
 * left (`backend/plans/node_states.py`, the same rule the agent's full read
 * uses); the fold wave is where the user sees it go.
 * Each level of the tree renders its still-open rows first, in plan order, so
 * its top row is always the current or next item; its finished rows (done,
 * failed or closed, and not blocked by an open descendant) sink to the bottom
 * of that same level, newest settled first; its folded rows page open below
 * those, also newest first; and one "N completed" row closes the level. That
 * row opens in pages of `FOLD_PAGE`. Folding and sinking move rows; ratios
 * come from the server, which counts only branches still in play.
 *
 * Timed waits order soonest-ending first after the level's other open rows.
 * Only finished work folds: an open task is never behind the fold row's check.
 */

/** Folded rows one "Show more" press reveals at a level. */
export const FOLD_PAGE = 5;

/** The level of a node whose parent is not in this tree (the plan's roots). */
export const ROOT_LEVEL = '';

export type PlanTreeItem =
	/** `nest` indents a row under its level's Scheduled row; `view` says how it words its time. */
	| { kind: 'node'; key: string; node: OperatorNode; nest?: number; view?: ScheduledView }
	| { kind: 'fold'; key: string; level: string; depth: number; folded: OperatorNode[]; shown: number }
	/** A level's quietly scheduled tasks (two or more), opened or closed as one. */
	| { kind: 'scheduled'; key: string; level: string; depth: number; members: OperatorNode[]; open: boolean }
	/** Members of an open Scheduled row that share one moment, under one time line. */
	| { kind: 'cluster'; key: string; depth: number; at: number; members: OperatorNode[] };

/** The level a node sits in: its parent's id, or the root level. */
export function levelOf(node: OperatorNode, byId: Map<string, OperatorNode>): string {
	return node.parent_id && byId.has(node.parent_id) ? node.parent_id : ROOT_LEVEL;
}

function settledAt(node: OperatorNode): number {
	const at = Date.parse(node.last_transition_at);
	return Number.isNaN(at) ? 0 : at;
}

const TERMINAL_TONES = new Set(['complete', 'failed', 'closed']);

/**
 * Whether a node reads finished (done, failed, or closed) for ordering and
 * folding. This is its effective display, so a never-started container reads
 * finished from its leaves; its raw lifecycle `state`, which actions act on,
 * is unchanged and is never read here.
 */
export function isFinished(node: OperatorNode): boolean {
	return TERMINAL_TONES.has(node.display.tone_key);
}

/**
 * Ids of terminal nodes that keep their plan-order place because a
 * descendant is still open: a finished parent whose subtree still holds open
 * work is not "finished" for ordering, mirroring the server's
 * `folded_subtrees` rule that a finished parent never folds over open work
 * (`backend/plans/node_states.py`).
 */
export function openBlockedIds(byId: Map<string, OperatorNode>): Set<string> {
	const blocked = new Set<string>();
	for (const node of byId.values()) {
		if (isFinished(node)) continue;
		const seen = new Set<string>([node.id]);
		let parentId = node.parent_id;
		while (parentId && byId.has(parentId) && !seen.has(parentId) && !blocked.has(parentId)) {
			blocked.add(parentId);
			seen.add(parentId);
			parentId = byId.get(parentId)?.parent_id ?? null;
		}
	}
	return blocked;
}

/** Each level's folded rows within the rendered depth, newest settled first. */
export function foldedByLevel(
	nodes: OperatorNode[],
	byId: Map<string, OperatorNode>,
	maxDepth: number,
): Map<string, OperatorNode[]> {
	const levels = new Map<string, OperatorNode[]>();
	for (const node of nodes) {
		if (!node.folded || node.depth > maxDepth) continue;
		const level = levelOf(node, byId);
		levels.set(level, [...(levels.get(level) ?? []), node]);
	}
	for (const folded of levels.values()) folded.sort((a, b) => settledAt(b) - settledAt(a));
	return levels;
}

/** Page each level open just far enough to show every folded row on `path`; nothing folds back. */
export function revealFoldedPath(
	nodes: OperatorNode[],
	byId: Map<string, OperatorNode>,
	maxDepth: number,
	path: string[],
	revealed: Record<string, number>,
): Record<string, number> {
	const levels = foldedByLevel(nodes, byId, maxDepth);
	const next = { ...revealed };
	for (const nodeId of path) {
		const node = byId.get(nodeId);
		if (!node?.folded) continue;
		const level = levelOf(node, byId);
		const position = levels.get(level)?.findIndex((candidate) => candidate.id === nodeId) ?? -1;
		if (position >= 0) next[level] = Math.max(next[level] ?? 0, position + 1);
	}
	return next;
}

/** The folded rows the user has paged open: the first `revealed[level]` of each level. */
export function revealedIds(
	levels: Map<string, OperatorNode[]>,
	revealed: Record<string, number>,
): Set<string> {
	const ids = new Set<string>();
	for (const [level, folded] of levels) {
		for (const node of folded.slice(0, revealed[level] ?? 0)) ids.add(node.id);
	}
	return ids;
}

/** The nearest ancestor that is rendered, so a row under a hidden parent keeps its place. */
function shownParent(
	node: OperatorNode,
	byId: Map<string, OperatorNode>,
	shown: Set<string>,
): string {
	const seen = new Set<string>([node.id]);
	let parentId = node.parent_id;
	while (parentId && byId.has(parentId) && !seen.has(parentId)) {
		if (shown.has(parentId)) return parentId;
		seen.add(parentId);
		parentId = byId.get(parentId)?.parent_id ?? null;
	}
	return ROOT_LEVEL;
}

export interface ArrangeInput {
	/** Rendered rows in canonical plan order, after every visibility filter. */
	visible: OperatorNode[];
	byId: Map<string, OperatorNode>;
	levels: Map<string, OperatorNode[]>;
	/** Whether a level's children are on screen: its parent is rendered and open. */
	isLevelOpen: (level: string) => boolean;
	/** Terminal nodes an open descendant keeps out of the finished group (`openBlockedIds`). */
	openBlocked: Set<string>;
	/** Epoch ms the tree is read at; without it nothing gathers into a Scheduled row. */
	now?: number;
	/** Whether a level's Scheduled row is open. */
	isScheduledOpen?: (level: string) => boolean;
}

/**
 * Render order: at every level its still-open rows in plan order, then its
 * finished-but-not-yet-folded rows newest settled first, then its revealed
 * folded rows (also newest first), then its one fold row -- so the top row of
 * every level is always its current or next item. Each row is followed by its
 * own subtree, so a level's fold row closes that level.
 */
export function arrangeItems(input: ArrangeInput): PlanTreeItem[] {
	const { visible, byId, levels, openBlocked } = input;
	const shown = new Set(visible.map((node) => node.id));
	const children = new Map<string, OperatorNode[]>();
	for (const node of visible) {
		const parent = shownParent(node, byId, shown);
		children.set(parent, [...(children.get(parent) ?? []), node]);
	}
	const items: PlanTreeItem[] = [];
	const emitted = new Set<string>();
	const parents = parentIdsOf(byId.values());
	const quiet = (node: OperatorNode) => input.now !== undefined && !parents.has(node.id) && isScheduledMember(node, input.now);
	const emit = (level: string) => {
		const rows = children.get(level) ?? [];
		const order = new Map((levels.get(level) ?? []).map((node, index) => [node.id, index]));
		const unfolded = rows.filter((node) => !order.has(node.id));
		const settled = (node: OperatorNode) => isFinished(node) && !openBlocked.has(node.id);
		const byEnd = (a: OperatorNode, b: OperatorNode) => (waitEndsAtMs(a) ?? Infinity) - (waitEndsAtMs(b) ?? Infinity);
		const open = unfolded.filter((node) => !settled(node));
		// Open work first, then timed waits soonest first, then waits with no end
		// time: a wait ending in minutes never sits below one that may never end.
		// A row reads by its tone: a task behind an unfinished prerequisite projects as
		// blocked yet reads "Waiting · After …", and sits with the other waits. Ready
		// work nobody is moving also reads Waiting ("Not started") but stays with the
		// open work: nothing but the agent's turn holds it.
		const waitsWithoutEnd = (node: OperatorNode) =>
			node.display.tone_key === 'waiting' && node.execution.condition !== 'ready'
			&& waitEndsAtMs(node) === null;
		const scheduled = open.filter(quiet);
		const timed = open.filter((node) => waitEndsAtMs(node) !== null && !quiet(node)).sort(byEnd);
		const work = open.filter((node) => waitEndsAtMs(node) === null && !waitsWithoutEnd(node));
		const untimed = open.filter(waitsWithoutEnd);
		const finished = unfolded.filter((node) => settled(node))
			.sort((a, b) => settledAt(b) - settledAt(a));
		const revealed = rows.filter((node) => order.has(node.id))
			.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
		const push = (node: OperatorNode, extra: { nest?: number; view?: ScheduledView } = {}) => {
			if (emitted.has(node.id)) return;
			emitted.add(node.id);
			items.push({ kind: 'node', key: node.id, node, ...extra });
			emit(node.id);
		};
		for (const node of [...work, ...timed, ...untimed]) push(node);
		// Quiet scheduled work: one row in place alone, a group from two. Leaves have no subtree to emit.
		if (scheduled.length === 1) push(scheduled[0], { view: 'clock' });
		if (scheduled.length >= 2) {
			for (const node of scheduled) emitted.add(node.id);
			const isOpen = input.isScheduledOpen?.(level) ?? false;
			const depth = scheduled[0].depth;
			items.push({ kind: 'scheduled', key: `scheduled:${level}`, level, depth, members: scheduled, open: isOpen });
			for (const cluster of isOpen ? scheduledClusters(scheduled) : []) {
				const [only] = cluster.members;
				if (cluster.members.length === 1) items.push({ kind: 'node', key: only.id, node: only, nest: 1, view: 'clock' });
				else items.push({ kind: 'cluster', key: `cluster:${level}:${Math.floor(cluster.at / 60_000)}`, depth, at: cluster.at, members: cluster.members });
			}
		}
		for (const node of [...finished, ...revealed]) push(node);
		const folded = levels.get(level) ?? [];
		if (folded.length > 0 && input.isLevelOpen(level)) {
			items.push({
				kind: 'fold', key: `fold:${level}`, level,
				depth: folded[0].depth, folded, shown: revealed.length,
			});
		}
	};
	emit(ROOT_LEVEL);
	// A malformed cyclic parent chain has no root to walk from; its rows still
	// render, in plan order, rather than vanish (R6).
	for (const node of visible) {
		if (emitted.has(node.id)) continue;
		emitted.add(node.id);
		items.push({ kind: 'node', key: node.id, node });
		emit(node.id);
	}
	return items;
}

/** What a fold row says: its finished work by outcome, completed first. */
export function foldLabel(folded: OperatorNode[]): string {
	const completed = folded.filter((node) => node.display.tone_key === 'complete').length;
	const failed = folded.filter((node) => node.display.tone_key === 'failed').length;
	const other = folded.length - completed - failed;
	const parts = [
		completed > 0 || folded.length === 0 ? `${completed} completed` : null,
		failed > 0 ? `${failed} failed` : null,
		other > 0 ? `${other} closed` : null,
	];
	return parts.filter((part) => part !== null).join(', ');
}

/** Whether a fold row may wear the done mark: only when all of it completed. */
export function foldAllCompleted(folded: OperatorNode[]): boolean {
	return folded.every((node) => node.display.tone_key === 'complete');
}
