import type { OperatorCostAmount, OperatorFoldLevel, OperatorNode } from '$lib/api/plans';
import type { FoldedItem } from '$lib/api/plansFolded';
import type { LevelHistory } from './foldHistory.svelte';
import { levelOf, ROOT_LEVEL } from './planTreeFold';

/**
 * What each level's fold row says and which of its finished rows can be opened.
 *
 * The server is the only fold authority (`plans.open_folds`): a level's counts
 * are the numbers it sent, never a count of the rows this read happened to
 * list. A read lists the open work and a window of folded rows; every older
 * folded row of a level is fetched on demand (`foldHistory`) and drawn from
 * the history route's thin row (`foldedNode`).
 */

/** What a plan tree needs of the server's folds: its levels, and where its history is read from. */
export interface FoldedWork {
	planId: string;
	levels: OperatorFoldLevel[];
	/** The node whose children form the top level once a sole wrapper is dropped (`withoutSoleWrapper`). */
	rootParent?: string | null;
}

/** What a tree reads of a plan entry's folds. */
export function foldedOf(entry: { plan: { id: string }; folds: OperatorFoldLevel[] }): FoldedWork {
	return { planId: entry.plan.id, levels: entry.folds };
}

export interface FoldCounts {
	done: number;
	failed: number;
	closed: number;
}

export interface FoldLevelView {
	level: string;
	/** The depth of the rows beneath it. */
	depth: number;
	/** Folded rows at this level by tone, not counting rows the pane holds open for now. */
	counts: FoldCounts;
	total: number;
	/** The rows that can be opened now, newest first: those the read listed, then those fetched. */
	rows: OperatorNode[];
	history: LevelHistory;
}

export interface FoldLevelsInput {
	/** The rows as shown: held rows already unfolded. */
	nodes: OperatorNode[];
	/** Rows the pane holds open (they are counted by the server, but are not under the fold row yet). */
	held: ReadonlySet<string>;
	work: FoldedWork | null;
	history: (level: string) => LevelHistory;
	maxDepth: number;
}

const EMPTY_AMOUNT: OperatorCostAmount = {
	tokens: { known: null, unknown_rows: 0 },
	model_cost_usd: { known: null, unknown_rows: 0 },
	usage_recording_failures: 0,
	execution: { seconds: 0, unknown_turns: 0, turns: { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 } },
	waiting: { seconds: 0 },
};

/** A history row as a tree row: what the route sent, and nothing it did not. */
export function foldedNode(item: FoldedItem, depth: number, parentId: string | null): OperatorNode {
	return {
		id: item.id, parent_id: parentId, depth, kind: item.kind, title: item.title,
		description: null, acceptance_criteria: '', state: item.state,
		unmet_dependencies: [], held_by: null, assignee: null,
		revision: 0, requirements_revision: 0, commitment_id: '',
		updated_at: item.folded_at, last_transition_at: item.folded_at, folded: true, requires_user_action: false,
		executor: { thread_id: '', label: '' },
		execution: {
			condition: 'terminal', primary_reason: null, causes: [], selection: null, conflict: null,
			attempt_count: 0, friction_count: 0, attempts: [],
		},
		display: item.display, status_reason: null, evidence: null, commitment_cost: null,
		node_cost: { own: EMPTY_AMOUNT, rolled: EMPTY_AMOUNT }, initiative: null,
		branch_progress: null, scheduled_until: null, attention_path: [], completion: null,
		child: null, review: null, user_actions: [], discussion_artifact_id: null, artifacts: [],
	};
}

export function foldTotal(counts: FoldCounts): number {
	return counts.done + counts.failed + counts.closed;
}

/** The tone a folded row is counted under: its displayed outcome (`fold_bucket` on the server). */
function toneOf(node: OperatorNode): keyof FoldCounts {
	const tone = node.display.tone_key;
	return tone === 'complete' ? 'done' : tone === 'failed' ? 'failed' : 'closed';
}

/** Whether a plan has any folded work at all, so a plan of only finished work is not read as empty. */
export function hasFolded(levels: readonly OperatorFoldLevel[]): boolean {
	return levels.some((level) => foldTotal(level) > 0);
}

/** Display order of rows the read listed; the server's order is not on the wire, so a row's last change stands in. */
function newestFirst(a: OperatorNode, b: OperatorNode): number {
	const at = (node: OperatorNode) => {
		const ms = Date.parse(node.last_transition_at);
		return Number.isNaN(ms) ? 0 : ms;
	};
	return at(b) - at(a) || a.id.localeCompare(b.id);
}

/**
 * Each level that has folded work, with its server counts and its openable rows,
 * plus the tree rows for every history row fetched (`extra`), so the tree can
 * place, expand and reveal them like any other row, and the levels past the
 * rendered depth (`deep`), whose parent offers the drill instead.
 */
export function buildFoldLevels(
	input: FoldLevelsInput,
): { levels: Map<string, FoldLevelView>; extra: OperatorNode[]; deep: string[] } {
	const { nodes, held, work, history, maxDepth } = input;
	const byId = new Map(nodes.map((node) => [node.id, node]));
	const counts = new Map<string, FoldCounts>();
	for (const entry of work?.levels ?? []) {
		counts.set(entry.parent_id ?? ROOT_LEVEL, { done: entry.done, failed: entry.failed, closed: entry.closed });
	}
	// The server counts every folded row, so a row held open for now is taken off its level's count.
	const listed = new Map<string, OperatorNode[]>();
	for (const node of nodes) {
		const level = levelOf(node, byId);
		if (node.folded) listed.set(level, [...(listed.get(level) ?? []), node]);
	}
	const heldNodes = nodes.filter((node) => held.has(node.id));
	const extra = new Map<string, OperatorNode>();
	const levelOfExtra = new Map<string, string>();
	const depthOf = (level: string): number => {
		if (level === ROOT_LEVEL) return 0;
		const parent = byId.get(level) ?? extra.get(level);
		if (parent) return parent.depth + 1;
		const above = levelOfExtra.get(level);
		return above === undefined ? 0 : depthOf(above) + 1;
	};
	const views = new Map<string, FoldLevelView>();
	/** Levels with folded work whose rows would sit past the rendered depth. */
	const deep: string[] = [];
	const visit = (level: string) => {
		const own = counts.get(level);
		if (!own) return;
		const shown = { ...own };
		for (const node of heldNodes) if (levelOf(node, byId) === level) shown[toneOf(node)] -= 1;
		const depth = depthOf(level);
		if (foldTotal(shown) <= 0) return;
		if (depth > maxDepth) {
			deep.push(level);
			return;
		}
		const past = history(level);
		const open = [...(listed.get(level) ?? [])].sort(newestFirst);
		const known = new Set(open.map((node) => node.id));
		const fetched: OperatorNode[] = [];
		for (const item of past.items) {
			if (known.has(item.id) || held.has(item.id)) continue;
			const node = byId.get(item.id) ?? foldedNode(item, depth, level === ROOT_LEVEL ? null : level);
			if (!byId.has(item.id)) {
				extra.set(item.id, node);
				levelOfExtra.set(item.id, level);
			}
			fetched.push(node);
			// A fetched row with folded rows beneath it opens a level of its own, paged the same way.
			if (item.has_folded_children && !counts.has(item.id)) counts.set(item.id, { ...item.folds });
		}
		views.set(level, { level, depth, counts: shown, total: foldTotal(shown), rows: [...open, ...fetched], history: past });
	};
	// Parents first: a fetched row's own level is only known once its parent level is read.
	const queue = [...counts.keys()];
	const seen = new Set<string>();
	while (queue.length > 0) {
		const level = queue.shift()!;
		if (seen.has(level)) continue;
		seen.add(level);
		visit(level);
		for (const key of counts.keys()) if (!seen.has(key)) queue.push(key);
	}
	return { levels: views, extra: [...extra.values()], deep };
}

/** What a fold row says: its finished work by outcome, completed first. */
export function foldLabel(counts: FoldCounts): string {
	const total = foldTotal(counts);
	const parts = [
		counts.done > 0 || total === 0 ? `${counts.done} completed` : null,
		counts.failed > 0 ? `${counts.failed} failed` : null,
		counts.closed > 0 ? `${counts.closed} closed` : null,
	];
	return parts.filter((part) => part !== null).join(', ');
}

/** Whether a fold row may wear the done mark: only when all of it completed. */
export function foldAllCompleted(counts: FoldCounts): boolean {
	return counts.failed === 0 && counts.closed === 0;
}

/** Rows a level still has to open: none past the end of its history, whatever the count says. */
export function foldRemaining(level: FoldLevelView): number {
	return level.history.status === 'end' ? Math.min(level.total, level.rows.length) : level.total;
}
