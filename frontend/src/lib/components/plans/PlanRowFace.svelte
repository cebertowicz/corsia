<!--
	The status mark and copy of one plan row.

	Branch rows, leaf rows and expanded child-plan rows all show the same face;
	only the surrounding container differs (a disclosure button, a plain span).
	Keeping the face in one component is what makes a branch and a leaf share one
	baseline and one left edge, and means a tone or label change lands in every
	row at once.
-->
<script lang="ts">
	import type { OperatorNode } from '$lib/api/plans';
	import { activeToolsByThread } from '$lib/stores/threads';
	import { minuteClock } from '$lib/time';
	import { turnStates } from '$lib/ws/turnState';
	import { nodeDisplayLabel, nodeDisplayState, nodeStateTone, nodeSublineParts, type ScheduledView } from './planDisplay';
	import { isOutsideWait, isScheduledTask } from './planWaits';
	import ThreadWaitingMark from '$lib/components/ThreadWaitingMark.svelte';
	import PlanStateIcon from './PlanStateIcon.svelte';
	import SnoozeClock from './SnoozeClock.svelte';
	import { planLiveStep } from './planLiveStep';
	import { createLiveLine } from './liveLine.svelte';
	import PlanLiveStep from './PlanLiveStep.svelte';

	// A branch row in the tree carries its count on its expand control instead.
	// threads, moves: this row's rolled work (`rolledLive`); carriedBelow: what rows rendered under it already show.
	let { node, isNext = false, showProgress = true, detailsOpen = false, threads = [], moves = false, owners, carriedBelow = [], scheduledView }: { node: OperatorNode; isNext?: boolean; showProgress?: boolean; detailsOpen?: boolean; threads?: readonly string[]; moves?: boolean; owners?: ReadonlyMap<string, OperatorNode>; carriedBelow?: readonly string[]; scheduledView?: ScheduledView } = $props();

	const state = $derived(nodeDisplayState(node));
	const tone = $derived(nodeStateTone(node));
	// A task the agent set aside until a date wears the snooze clock; other waits keep the dot.
	const scheduled = $derived(state === 'waiting' && isScheduledTask(node));
	// An outside wait wears the hourglass, so it no longer reads as an inside wait's dot (EW11).
	const outsideWait = $derived(state === 'waiting' && !scheduled && isOutsideWait(node));
	const label = $derived(nodeDisplayLabel(node, isNext));
	// A narrow row cuts the reason first; a settled row's time stays whole.
	const subline = $derived(nodeSublineParts(node, $minuteClock, scheduledView));
	// AS14: running work reads its current step on the row itself.
	const liveNow = $derived(planLiveStep(threads, $turnStates, $activeToolsByThread));
	const liveLine = createLiveLine(
		() => liveNow,
		() => node.execution.condition === 'terminal',
	);
	// The line sits on the lowest visible row carrying its thread; the spin never depends on where it sits.
	const liveStep = $derived(liveLine.shown && !carriedBelow.includes(liveLine.shown.threadId) ? liveLine.shown : null);
	const moving = $derived(
		state === 'working' && (node.execution.condition === 'running' || moves || liveLine.shown !== null),
	);
	const branchProgress = $derived(
		showProgress && node.branch_progress && node.branch_progress.total > 0
			? `${node.branch_progress.done}/${node.branch_progress.total}`
			: null,
	);
	const symbol = $derived.by(() => {
		if (state === 'needs-you') return '!';
		if (state === 'waiting') return isNext ? '→' : '•';
		if (state === 'finished') {
			if (tone === 'failed') return '×';
			if (tone === 'closed') return '–';
			return '✓';
		}
		return isNext ? '→' : '';
	});
</script>

<PlanStateIcon {state} {tone} {label} spinning={moving}>{#if scheduled}<SnoozeClock />{:else if outsideWait}<ThreadWaitingMark size={12} />{:else}{symbol}{/if}</PlanStateIcon>
<span class="row-copy">
	<span class="row-line">
		<span class="row-title" class:details-open={detailsOpen} title={node.title}>{node.title}</span>
		{#if branchProgress}<span class="branch-progress">{branchProgress}<span class="sr-only">{' done'}</span></span>{/if}
		<span class="row-status sr-only">{label}</span>
	</span>
	{#if liveStep}
		<span class="row-subline {state} {tone}"><PlanLiveStep step={liveStep} node={owners?.get(liveStep.threadId) ?? node} />{#if subline.lead}<span class="subline-text">{' · '}{subline.lead}</span>{/if}{#if subline.moment}<span class="subline-moment">{' · '}{subline.moment}</span>{/if}</span>
	{:else if subline.lead || subline.moment}<span class="row-subline {state} {tone}">{#if subline.lead}<span class="subline-text">{subline.lead}</span>{/if}{#if subline.moment}<span class="subline-moment">{subline.lead ? ' · ' : ''}{subline.moment}</span>{/if}</span>{/if}
</span>

<style>
	.row-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; }
	.row-title {
		overflow: hidden; color: var(--text-primary); font-size: var(--text-subhead);
		font-weight: var(--fw-medium); line-height: var(--lh-normal); text-overflow: ellipsis; white-space: nowrap;
	}
	/* One line: the icon carries status, the title truncates, progress never wraps. */
	.row-line { display: flex; min-width: 0; align-items: baseline; gap: var(--space-2); }
	.row-title { flex: 0 1 auto; min-width: 0; }
	/* Open details make the same title readable in place; closed rows remain compact. */
	.row-title.details-open { flex: 1 1 0; min-width: 0; overflow: visible; text-overflow: clip; white-space: normal; }
	.branch-progress {
		flex: none; color: var(--text-tertiary); font-size: var(--text-caption);
		font-variant-numeric: tabular-nums;
	}
	/* One line of running text that truncates once, at its end: the live step, the lead and the moment
	   are inline runs, so two parts never each show their own ellipsis. */
	.row-subline {
		display: block; min-width: 0; overflow: hidden; color: var(--text-tertiary);
		font-size: var(--text-caption); text-overflow: ellipsis; white-space: nowrap;
	}
	.row-subline :global(.live-step) { display: inline; }
	.subline-text, .subline-moment { white-space: pre; }
	.row-subline.needs-you { color: var(--amber-text); }
	.row-subline.waiting { color: var(--text-secondary); }
	.row-subline.finished.failed { color: var(--red); }
	.sr-only {
		position: absolute; width: 1px; height: 1px; overflow: hidden;
		clip: rect(0 0 0 0); white-space: nowrap;
	}
</style>
