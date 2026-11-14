<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import { isYourMove } from '$lib/components/threadRowMark';
	import { isMobile } from '$lib/stores/viewport';
	import { planQuery } from '$lib/stores/planQuery.svelte';
	import type { OperatorKey } from '$lib/stores/planKeys';
	import { threadPlanStates, watchOpenThreadPlanStates } from '$lib/stores/threadPlanStates';
	import { hasFolded } from './planFoldLevels';
	import ThreadPlanPane from './ThreadPlanPane.svelte';
	import PlanHeadControls from './PlanHeadControls.svelte';
	import { registerPlanSurface } from '$lib/planNodeFocus';

	let {
		threadId,
		onOpenChild = () => {},
		onOpenLegacyDiscussion = () => {},
	}: {
		threadId: string;
		onOpenChild?: (nodeId: string, threadId: string, title: string, stepId?: string) => void;
		onOpenLegacyDiscussion?: (artifactId: string, title: string) => void;
	} = $props();

	const key: OperatorKey = $derived({ kind: 'thread', id: threadId });
	const query = planQuery(() => key);
	const plan = $derived(query.payload?.plans[0] ?? null);
	// A plan whose work has all folded reads 0/0 but still has steps to open.
	const visible = $derived($isMobile && !!plan && (plan.nodes.length > 0 || hasFolded(plan.folds)));
	// The phone hides the menu, so this chip shows the menu row's own needs-you mark:
	// the same rule on the same row state, so pausing the item lifts both together.
	const needsYou = $derived(isYourMove($threadPlanStates[threadId]));
	const chipLabel = $derived(plan && plan.progress.open > 0
		? `Open plan — ${plan.progress.open} left`
		: 'Open plan — all work complete');
	let open = $state(false);
	let openedFor = $state<string | null>(null);

	// The menu may not list this thread at all (a hidden subagent's chat, another
	// scope), so the store reads its row state for this chip; the menu's dot is
	// derived from its listed set and stays untouched.
	$effect(() => {
		watchOpenThreadPlanStates([threadId]);
		return () => watchOpenThreadPlanStates([]);
	});

	$effect(() => {
		if (threadId === openedFor) return;
		openedFor = threadId;
		open = false;
	});

	$effect(() => {
		if (!$isMobile && open) open = false;
	});

	// A plan-node request for this thread opens the drawer even before the plan has loaded:
	// its pane shows the loading state and holds the node until the plan arrives (`planNodeFocus`).
	$effect(() => registerPlanSurface((id) => {
		if (!$isMobile || id !== threadId) return false;
		open = true;
		return true;
	}));

	function openChild(nodeId: string, childThreadId: string, title: string, stepId?: string): void {
		open = false;
		onOpenChild(nodeId, childThreadId, title, stepId);
	}

	function openLegacyDiscussion(artifactId: string, title: string): void {
		open = false;
		onOpenLegacyDiscussion(artifactId, title);
	}
</script>

{#if visible && plan}
	<button
		class="plan-progress-chip press-tint"
		class:needs-you={needsYou}
		type="button"
		aria-label={needsYou ? `${chipLabel} · Needs you` : chipLabel}
		aria-haspopup="dialog"
		aria-expanded={open}
		onclick={() => (open = true)}
	>
		<span aria-hidden="true" class="progress-mark"></span>
		<b>{plan.progress.open > 0 ? `${plan.progress.open} left` : '✓'}</b>
	</button>
{/if}
{#if open}
	<AdaptiveDrawer
		ariaLabel={!plan ? 'Plan' : plan.progress.open > 0 ? `Plan — ${plan.progress.open} left` : 'Plan — all work complete'}
		heading="Plan"
		headingMeta={plan && plan.progress.open > 0 ? `${plan.progress.open} left` : null}
		onClose={() => (open = false)}
	>
		{#snippet leading()}<span class="drawer-pause"><PlanHeadControls {threadId} show="pause" /></span>{/snippet}
		<div class="drawer-plan">
			<!-- The drawer header owns the title, count and Pause; the pane holds only the plan. -->
			<ThreadPlanPane
				{threadId}
				onOpenChild={openChild}
				onOpenLegacyDiscussion={openLegacyDiscussion}
			/>
		</div>
	</AdaptiveDrawer>
{/if}

<style>
	/*
	 * One peer of the mobile action rail: same 44px interaction height, same
	 * canonical glass material, same elevation-1 chip family and pill radius as
	 * the live-edge and host-access controls beside it.
	 */
	.plan-progress-chip {
		min-width: 58px;
		height: 44px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: var(--space-1);
		padding: 0 var(--space-4);
		border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
		border-radius: var(--radius-pill);
		background: var(--glass-regular-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-elevation-1), var(--glass-rim);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-footnote);
		font-variant-numeric: tabular-nums;
		cursor: pointer;
	}
	.progress-mark {
		width: 7px;
		height: 7px;
		border-radius: var(--radius-circle);
		--mark-colour: var(--accent);
		background: var(--mark-colour);
		box-shadow: 0 0 0 4px color-mix(in srgb, var(--mark-colour) 13%, transparent);
	}
	/*
	 * The menu row's needs-you amber (ThreadPlanStateMark's dot and word) on the
	 * chip's own dot and halo, so the chip and the row it stands for read as one fact.
	 */
	.plan-progress-chip.needs-you .progress-mark { --mark-colour: var(--amber); }
	.plan-progress-chip.needs-you b { color: var(--amber-text); }
	.plan-progress-chip:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 3px;
	}
	.drawer-plan {
		padding: var(--space-1) 0 var(--space-5);
	}
	/* The same touch minimum Done has in the drawer chrome. */
	@media (max-width: 620px) {
		.drawer-pause :global(button) { min-height: 44px; }
	}
</style>
