<script lang="ts">
	import { enterProjectScope } from '$lib/stores/projects';
	import type { ActivityGroupSummary, ActivityPlanGroup } from '$lib/activityHome';
	import ActivityPlanRow from './ActivityPlanRow.svelte';

	/** One project's rows in one of the In progress, Waiting or Complete sections. */
	let {
		group,
		kind,
		showHead,
		onSelectThread,
		onOpenDecision = () => {},
		onOpenChild = () => {},
		onOpenLegacyDiscussion = () => {},
	}: {
		group: ActivityPlanGroup;
		kind: 'current' | 'waiting' | 'complete';
		/** False inside a project scope, where the scope itself is the heading. */
		showHead: boolean;
		onSelectThread: (threadId: string) => void | Promise<void>;
		/** Opens a row's thread with its decision expanded. */
		onOpenDecision?: (threadId: string) => void | Promise<void>;
		onOpenChild?: (
			parentThreadId: string,
			nodeId: string | null,
			childThreadId: string,
			title: string,
			stepId?: string,
		) => void | Promise<void>;
		onOpenLegacyDiscussion?: (
			parentThreadId: string,
			artifactId: string,
			title: string,
		) => void | Promise<void>;
	} = $props();

	const MAX_VISIBLE_ROWS = 4;

	const rows = $derived(
		kind === 'current' ? group.current : kind === 'waiting' ? group.waiting : group.complete,
	);
	let expanded = $state(false);
	const visible = $derived(expanded ? rows : rows.slice(0, MAX_VISIBLE_ROWS));

	/**
	 * The group's compact distribution, omitting zero-valued classes rather
	 * than printing `0`. Counts come from `activityHome`'s summary, which sums
	 * the server's own per-plan progress — never a second counting path.
	 */
	function summaryText(summary: ActivityGroupSummary): string {
		if (kind === 'waiting') return `${summary.waiting} waiting`;
		if (kind === 'complete') return `${rows.length} complete`;
		const parts: string[] = [];
		if (summary.moving > 0) parts.push(`${summary.moving} moving`);
		if (summary.needsYou > 0) parts.push(`${summary.needsYou} needs you`);
		if (summary.left > 0) parts.push(`${summary.left} left`);
		return parts.join(' · ');
	}
</script>

<section class="activity-group">
	{#if showHead}
		<header class="group-head">
			{#if group.projectId && group.label}
				<!-- The existing group label is the canonical jump affordance;
				     Activity adds no second scope-selection owner. Offered only
				     with a resolved name, so navigation can never snapshot a
				     placeholder as the scope's name. -->
				<h3>
					<button
						class="group-jump"
						type="button"
						onclick={() => enterProjectScope({ id: group.projectId!, name: group.label! })}
					>{group.label}</button>
				</h3>
			{:else}
				<h3>{group.label ?? 'Project'}</h3>
			{/if}
			<span class="group-summary">{summaryText(group.summary)}</span>
		</header>
	{/if}

	<div class="group-rows" class:complete={kind === 'complete'}>
		{#each visible as row (row.entry.plan.id)}
			<ActivityPlanRow {row} {onSelectThread} {onOpenDecision} {onOpenChild} {onOpenLegacyDiscussion} />
		{/each}
	</div>

	{#if rows.length > MAX_VISIBLE_ROWS}
		<button class="more-toggle" onclick={() => (expanded = !expanded)}>
			{expanded ? 'Show less' : 'Show ' + (rows.length - MAX_VISIBLE_ROWS) + ' more'}
		</button>
	{/if}

</section>

<style>
	.group-rows { display: flex; flex-direction: column; }
	.activity-group { padding: var(--space-3) 0; border-top: 1px solid var(--border); }
	.group-head {
		display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-2);
		margin-bottom: var(--space-1);
	}
	.group-head h3 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); font-weight: var(--fw-semibold); }
	/* The label itself is the target: same type, no new chrome. */
	.group-jump {
		padding: 0; border: 0; background: none; color: inherit; font: inherit;
		letter-spacing: inherit; cursor: pointer;
	}
	.group-jump:focus-visible {
		outline: 2px solid var(--accent); outline-offset: 3px; border-radius: var(--radius-xs);
	}
	.group-summary { color: var(--text-tertiary); font-size: var(--text-caption); }
	.more-toggle {
		display: flex; align-items: center; gap: var(--space-1); min-height: 38px; padding: var(--space-1) 0;
		border: 0; background: transparent; color: var(--text-tertiary); font: inherit;
		font-size: var(--text-caption); cursor: pointer;
	}
	.more-toggle:focus-visible {
		outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs);
	}
	.group-rows.complete { opacity: 0.82; }
	/* Hover colour is for a pointer; a tap leaves :hover stuck on the last target. */
	@media (hover: hover) {
		.group-jump:hover { color: var(--accent-text); }
		.more-toggle:hover { color: var(--text-primary); }
	}
	@media (max-width: 620px) {
		.more-toggle, .group-jump { min-height: 44px; }
	}
</style>
