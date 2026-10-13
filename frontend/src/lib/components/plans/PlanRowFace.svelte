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
	import { isScheduledTask } from './planWaits';
	import SnoozeClock from './SnoozeClock.svelte';
	import { planLiveStep } from './planLiveStep';
	import { createLiveLine } from './liveLine.svelte';
	import PlanLiveStep from './PlanLiveStep.svelte';

	// A branch row in the tree carries its count on its expand control instead.
	// threads, moves: this row's rolled work (`rolledLive`); carriedBelow: what rows rendered under it already show.
	let { node, isNext = false, showProgress = true, detailsOpen = false, threads = [], moves = false, carriedBelow = [], scheduledView }: { node: OperatorNode; isNext?: boolean; showProgress?: boolean; detailsOpen?: boolean; threads?: readonly string[]; moves?: boolean; carriedBelow?: readonly string[]; scheduledView?: ScheduledView } = $props();

	const state = $derived(nodeDisplayState(node));
	const tone = $derived(nodeStateTone(node));
	// A task the agent set aside until a date wears the snooze clock; other waits keep the dot.
	const scheduled = $derived(state === 'waiting' && isScheduledTask(node));
	const label = $derived(nodeDisplayLabel(node, isNext));
	// A narrow row cuts the reason first; when a settled row settled stays whole.
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

<span class="state-icon {state} {tone}" class:spinning={moving} title={label} aria-hidden="true">{#if scheduled}<SnoozeClock />{:else}{symbol}{/if}</span>
<span class="row-copy">
	<span class="row-line">
		<span class="row-title" class:details-open={detailsOpen} title={node.title}>{node.title}</span>
		{#if branchProgress}<span class="branch-progress">{branchProgress}<span class="sr-only">{' done'}</span></span>{/if}
		<span class="row-status sr-only">{label}</span>
	</span>
	{#if liveStep}
		<span class="row-subline {state} {tone}"><PlanLiveStep step={liveStep} />{#if subline.lead}<span class="subline-text">{' · '}{subline.lead}</span>{/if}{#if subline.moment}<span class="subline-moment">{' · '}{subline.moment}</span>{/if}</span>
	{:else if subline.lead || subline.moment}<span class="row-subline {state} {tone}">{#if subline.lead}<span class="subline-text">{subline.lead}</span>{/if}{#if subline.moment}<span class="subline-moment">{subline.lead ? ' · ' : ''}{subline.moment}</span>{/if}</span>{/if}
</span>

<style>
	.state-icon {
		display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px;
		flex: none; border: 1px solid var(--border); border-radius: var(--radius-circle);
		color: var(--text-secondary); font-size: var(--text-caption); font-weight: var(--fw-bold);
		line-height: 1;
	}
	.state-icon.working {
		border-color: var(--status-in-progress); color: var(--status-in-progress);
	}
	.state-icon.working.spinning { border-top-color: transparent; }
	.state-icon.needs-you { border-color: var(--amber); color: var(--amber); }
	.state-icon.waiting { border-color: var(--border); color: var(--text-secondary); }
	.state-icon.finished.failed { border-color: var(--red); color: var(--red); }
	.state-icon.finished.complete { border-color: var(--green); background: var(--green); color: var(--bg-primary); }
	/* Closed is neither success nor failure: quiet, like work that left the plan. */
	.state-icon.finished.closed { border-color: var(--border); color: var(--text-tertiary); }
	/* The one motion mechanism: only an actually-advancing row spins. */
	.state-icon.spinning { animation: plan-spin 1.2s linear infinite; }
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
	.row-subline {
		display: flex; min-width: 0; overflow: hidden; color: var(--text-tertiary);
		font-size: var(--text-caption); text-overflow: ellipsis; white-space: nowrap;
	}
	/* A flex row shows no ellipsis on bare text, so the text gets its own block. */
	.subline-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: pre; }
	.subline-moment { flex: none; white-space: pre; }
	.row-subline.needs-you { color: var(--amber); }
	.row-subline.waiting { color: var(--text-secondary); }
	.row-subline.finished.failed { color: var(--red); }
	.sr-only {
		position: absolute; width: 1px; height: 1px; overflow: hidden;
		clip: rect(0 0 0 0); white-space: nowrap;
	}
	@keyframes plan-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.state-icon.spinning { animation: none; }
	}
</style>
