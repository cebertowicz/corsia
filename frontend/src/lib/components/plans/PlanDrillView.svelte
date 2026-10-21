<script lang="ts">
	import type { OperatorArtifactLink, OperatorNode } from '$lib/api/plans';
	import type { FoldedWork } from './planFoldLevels';
	import PlanTree from './PlanTree.svelte';
	import { createChildExpansion } from './childExpansion.svelte';
	import type { PlanKey } from '$lib/stores/planKeys';

	let {
		nodes, folded = null, rootId, currentNodeIds = [], nextNodeIds = [], hostKey = null, onBack,
		onOpenChild = () => {},
		onOpenArtifact = () => {},
		onOpenLegacyDiscussion = () => {},
	}: {
		nodes: OperatorNode[];
		/** The plan's folds; the drilled subtree reads those beneath its root. */
		folded?: FoldedWork | null;
		rootId: string;
		currentNodeIds?: string[];
		nextNodeIds?: string[];
		/** The observed key (usually a thread) whose payload embeds these nodes'
		 * child progress — forwarded so a child-owner nudge also refreshes it. */
		hostKey?: PlanKey | null;
		onBack: () => void;
		onOpenChild?: (nodeId: string, threadId: string, title: string, stepId?: string) => void;
		onOpenArtifact?: (artifact: OperatorArtifactLink, nodeTitle: string) => void;
		onOpenLegacyDiscussion?: (artifactId: string, title: string) => void;
	} = $props();

	const childExpansion = createChildExpansion(() => hostKey);

	/** Terminates on a cyclic parent_id: a node is visited at most once (R6).
	 * Preserves the original array's order — never rebuilds via a queue (that
	 * would reorder siblings' subtrees, corrupting the server's canonical DFS). */
	function isDescendantOfRoot(node: OperatorNode, byId: Map<string, OperatorNode>): boolean {
		const seen = new Set<string>();
		let current: OperatorNode | undefined = node;
		while (current && !seen.has(current.id)) {
			if (current.id === rootId) return true;
			seen.add(current.id);
			current = current.parent_id ? byId.get(current.parent_id) : undefined;
		}
		return false;
	}

	function subtree(): OperatorNode[] {
		const byId = new Map(nodes.map((n) => [n.id, n]));
		const root = byId.get(rootId);
		if (!root) return [];
		// The drilled root is the subtree's own heading, so it never sits under a fold row.
		return nodes
			.filter((n) => isDescendantOfRoot(n, byId))
			.map((n) => ({ ...n, depth: n.depth - root.depth, folded: n.id === rootId ? false : n.folded }));
	}

	/** The folds beneath the drilled root: those whose parent is one of its rows. */
	function subtreeFolds(): FoldedWork | null {
		if (!folded) return null;
		const ids = new Set(focusedNodes.map((n) => n.id));
		return { ...folded, levels: folded.levels.filter((level) => level.parent_id !== null && ids.has(level.parent_id)) };
	}

	const rootNode = $derived(nodes.find((n) => n.id === rootId));
	const focusedNodes = $derived(subtree());
	const focusedFolds = $derived(subtreeFolds());

	let tree = $state<PlanTree | null>(null);

	/** Open what hides one row of the drilled subtree (`PlanTree.revealNode`). */
	export function revealNode(nodeId: string): boolean {
		return tree?.revealNode(nodeId) ?? false;
	}

	/** Open an older finished row of the drilled subtree (`PlanTree.revealFolded`). */
	export async function revealFolded(nodeId: string): Promise<boolean> {
		return (await tree?.revealFolded(nodeId)) ?? false;
	}

	/** Focus one rendered row of the drilled subtree (`PlanTree.focusRow`). */
	export function focusRow(nodeId: string): boolean {
		return tree?.focusRow(nodeId) ?? false;
	}
</script>

<div class="drill-view">
	<button class="drill-back press-tint" onclick={onBack}>
		<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
		<span>Back</span>
	</button>
	{#if rootNode}<h4 class="drill-title">{rootNode.title}</h4>{/if}
	<PlanTree
		bind:this={tree}
		nodes={focusedNodes}
		folded={focusedFolds}
		{currentNodeIds}
		{nextNodeIds}
		maxDepth={Infinity}
		expandBranchesByDefault={true}
		isChildExpanded={childExpansion.isExpanded}
		childRowState={childExpansion.state}
		onToggleChild={childExpansion.toggle}
		onRetryChild={childExpansion.retry}
		{onOpenChild}
		{onOpenArtifact}
		{onOpenLegacyDiscussion}
	/>
</div>

<style>
	.drill-view { display: flex; flex-direction: column; gap: var(--space-2); }
	.drill-back {
		display: inline-flex; align-items: center; gap: 4px; align-self: flex-start;
		border: 0; background: transparent; color: var(--text-secondary);
		font-size: var(--text-footnote); font-weight: var(--fw-medium); cursor: pointer;
		padding: var(--space-1) var(--space-2); border-radius: var(--radius-xs);
	}
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last thing tapped. */
	@media (hover: hover) {
		.drill-back:hover { background: var(--bg-hover); color: var(--text-primary); }
	}
	.drill-title { margin: 0; color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-semibold); }
</style>
