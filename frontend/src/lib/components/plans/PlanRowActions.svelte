<!--
	The trailing controls of one plan row: the branch expander, the review-item
	opener, the preserved-discussion link and the subagent controls. Split from
	`PlanTree` so the tree stays one file of row rules. They are one fixed group
	that never wraps: a narrow pane truncates the title instead, so every row
	stays one line with the controls in the same place.
-->
<script lang="ts">
	import type { OperatorArtifactLink, OperatorNode } from '$lib/api/plans';
	import PlanAskOpen from './PlanAskOpen.svelte';
	import PlanChildControls from './PlanChildControls.svelte';

	let {
		node, isBranch, branchExpanded, controls, ask, childExpanded,
		onToggleBranch, onOpenArtifact, onOpenLegacyDiscussion, onToggleChild, onOpenChild,
	}: {
		node: OperatorNode;
		/** Whether the row has something to disclose (children or a fold row). */
		isBranch: boolean;
		branchExpanded: boolean;
		/** Ids of the rows the expander controls, while it is open. */
		controls: string | undefined;
		ask: OperatorArtifactLink | undefined;
		childExpanded: boolean;
		onToggleBranch: () => void;
		onOpenArtifact: (artifact: OperatorArtifactLink, nodeTitle: string) => void;
		onOpenLegacyDiscussion: (artifactId: string, title: string) => void;
		onToggleChild: () => void;
		onOpenChild: () => void;
	} = $props();
</script>

<span class="row-actions">
{#if isBranch}
	<button
		class="branch-toggle"
		class:expanded={branchExpanded}
		type="button"
		aria-expanded={branchExpanded}
		aria-controls={controls}
		aria-label={`${branchExpanded ? 'Collapse' : 'Expand'} ${node.title}${node.branch_progress?.total ? `, ${node.branch_progress.done} of ${node.branch_progress.total} done` : ''}`}
		onclick={onToggleBranch}
	>
		{#if node.branch_progress && node.branch_progress.total > 0}
			<span class="branch-progress">{node.branch_progress.done}/{node.branch_progress.total}</span>
		{/if}
		<span class="branch-chevron" aria-hidden="true">
			<svg viewBox="0 0 20 20"><path d="m7.5 4.5 5 5-5 5" /></svg>
		</span>
	</button>
{/if}
{#if ask}<PlanAskOpen artifact={ask} nodeTitle={node.title} onOpen={onOpenArtifact} />{/if}
{#if node.discussion_artifact_id}
	<button
		class="discussion-archive"
		type="button"
		title="Open preserved discussion read-only"
		onclick={(event) => {
			event.stopPropagation();
			onOpenLegacyDiscussion(node.discussion_artifact_id!, node.title);
		}}
	>Discussion archive</button>
{/if}
{#if node.child?.available}
	<PlanChildControls {node} expanded={childExpanded} onToggle={onToggleChild} onOpen={onOpenChild} />
{/if}
</span>

<style>
	.row-actions { display: inline-flex; flex: none; flex-wrap: nowrap; justify-content: flex-end; align-items: center; gap: var(--space-2); margin-left: auto; }
	.branch-chevron {
		display: inline-flex; width: 20px; height: 20px; flex: none;
		align-items: center; justify-content: center; color: var(--text-tertiary);
	}
	.branch-chevron svg {
		width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8;
		stroke-linecap: round; stroke-linejoin: round; transition: transform var(--dur-tight) var(--ease-snap);
	}
	.branch-toggle.expanded .branch-chevron svg { transform: rotate(90deg); }
	.branch-toggle {
		display: inline-flex; flex: none; align-items: center; gap: var(--space-1);
		min-height: 28px; padding: 0 0 0 var(--space-2); border: 0; border-radius: var(--radius-xs);
		background: transparent; color: var(--text-tertiary); font: inherit; font-size: var(--text-caption);
		font-variant-numeric: tabular-nums; cursor: pointer;
	}
	.discussion-archive {
		flex: none; border: 0; border-radius: var(--radius-xs); background: transparent;
		color: var(--text-tertiary); padding: var(--space-1) var(--space-2);
		font: inherit; font-size: var(--text-caption); cursor: pointer;
	}
	.branch-toggle:hover, .discussion-archive:hover { color: var(--text-primary); background: var(--bg-hover); }
	.branch-toggle:focus-visible, .discussion-archive:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
	@media (max-width: 620px) {
		.branch-toggle, .discussion-archive { min-height: 44px; }
		.branch-toggle { min-width: 44px; justify-content: flex-end; }
	}
</style>
