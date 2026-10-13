import type { OperatorNode } from '$lib/api/plans';
import { tick } from 'svelte';
import { ancestorChain } from './planTreeFilter';
import { FOLD_PAGE, revealFoldedPath, type PlanTreeItem } from './planTreeFold';
import type { FoldLevelView } from './planFoldLevels';

interface FoldHistoryController {
	loadMore(level: string): Promise<void>;
	openPath(nodeId: string, hasNode: (nodeId: string) => boolean): Promise<boolean>;
}

interface PlanTreeRevealOptions {
	maxDepth: () => number;
	byId: () => Map<string, OperatorNode>;
	levels: () => Map<string, FoldLevelView>;
	items: () => PlanTreeItem[];
	openBranches(nodeIds: string[]): void;
	history: FoldHistoryController;
}

/**
 * What the person has revealed in a plan tree. Person-requested paging owns
 * block-slot motion; focus, fold waves, and live updates keep their own motion.
 */
export function createPlanTreeReveal({ maxDepth, byId, levels, items, openBranches, history }: PlanTreeRevealOptions) {
	let revealed = $state<Record<string, number>>({});
	let scheduledOpen = $state<Record<string, boolean>>({});
	let userPaging = $state(false);
	let pendingFoldLevel = $state<string | null>(null);

	function byRequest(change: () => void): void {
		userPaging = true;
		change();
		// Held past the frame that renders the change: rows read it as they enter and leave.
		void tick().then(() => requestAnimationFrame(() => requestAnimationFrame(() => (userPaging = false))));
	}

	function isScheduledOpen(level: string): boolean {
		return scheduledOpen[level] === true;
	}

	function revealNode(nodeId: string): boolean {
		const currentById = byId();
		if ((currentById.get(nodeId)?.depth ?? Infinity) > maxDepth()) return false;
		const ancestors = ancestorChain(currentById, nodeId).slice(1);
		openBranches(ancestors);
		revealed = revealFoldedPath(levels(), currentById, [nodeId, ...ancestors], revealed);
		const hiding = items().find((item) => item.kind === 'scheduled' && !item.open && item.members.some((member) => member.id === nodeId));
		if (hiding?.kind === 'scheduled') scheduledOpen = { ...scheduledOpen, [hiding.level]: true };
		return true;
	}

	async function revealFolded(nodeId: string): Promise<boolean> {
		if (byId().has(nodeId)) return revealNode(nodeId);
		if (!(await history.openPath(nodeId, (id) => byId().has(id)))) return false;
		return revealNode(nodeId);
	}

	async function pageFold(level: string): Promise<void> {
		const { rows = [], total = 0 } = levels().get(level) ?? {};
		const target = Math.min(total, Math.min(revealed[level] ?? 0, rows.length) + FOLD_PAGE);
		if (target > rows.length) {
			pendingFoldLevel = level;
			try {
				await history.loadMore(level);
				// A short final page still reveals once it arrives; it never needs a second press.
				const loaded = levels().get(level)?.rows.length ?? 0;
				if (loaded === 0) return;
				byRequest(() => (revealed = { ...revealed, [level]: Math.min(target, loaded) }));
			} finally {
				pendingFoldLevel = null;
			}
			return;
		}
		byRequest(() => (revealed = { ...revealed, [level]: target }));
	}

	function collapseFold(level: string): void {
		byRequest(() => (revealed = { ...revealed, [level]: 0 }));
	}

	function openScheduled(level: string): void {
		byRequest(() => (scheduledOpen = { ...scheduledOpen, [level]: true }));
	}

	function collapseScheduled(level: string): void {
		byRequest(() => (scheduledOpen = { ...scheduledOpen, [level]: false }));
	}

	return {
		get revealed() { return revealed; },
		get userPaging() { return userPaging; },
		get pendingFoldLevel() { return pendingFoldLevel; },
		pageSize: FOLD_PAGE,
		isScheduledOpen,
		revealNode,
		revealFolded,
		pageFold,
		collapseFold,
		openScheduled,
		collapseScheduled,
	};
}
