<!--
	The trailing subagent controls of one plan row: the chip that expands the
	child plan's subtasks in place, and the button that opens the subagent
	beside this thread. Split from `PlanTree` so the tree stays one file of
	row rules; both controls are siblings of the row's disclosure button, never
	nested in it.
-->
<script lang="ts">
	import type { OperatorNode } from '$lib/api/plans';
	import { livenessTone, livenessWord } from './planDisplay';
	import { waitBack, waitChecksBack } from './planWaits';
	import { minuteClock } from '$lib/time';
	import ThreadWaitingMark from '$lib/components/ThreadWaitingMark.svelte';

	let {
		node,
		expanded,
		onToggle,
		onOpen,
	}: {
		node: OperatorNode;
		expanded: boolean;
		onToggle: () => void;
		onOpen: () => void;
	} = $props();

	/**
	 * The child's tone, from the shared mapping so a subagent awaiting review is
	 * never announced as idle — the plan-level defect, one level down.
	 */
	function childActivity(): string {
		const liveness = node.child?.progress.liveness;
		return liveness ? livenessTone(liveness) : 'idle';
	}

	function childActivityLabel(): string {
		const child = node.child;
		if (!child) return 'idle';
		const { liveness, done, total } = child.progress;
		if (liveness === 'idle' && done === total) {
			return total > 0 ? 'done' : 'idle';
		}
		return livenessWord(liveness);
	}

	/**
	 * A child plan publishes `available: true` with raw progress the moment it
	 * exists, so a just-spawned subagent has nothing left yet. That is the chip's
	 * first state, not an edge case: show no count until there is one to show.
	 */
	const hasLeft = $derived((node.child?.progress.open ?? 0) > 0);
	// A waiting child says what it waits on and when its agent checks back (EW9). Its state stays its liveness.
	const childWait = $derived(node.child?.progress.liveness === 'waiting' ? node.child.wait ?? null : null);
	const waitMore = $derived(childWait && childWait.more > 0 ? ` +${childWait.more} more` : '');
	// The accessible name and title keep the word the glyph stands for.
	const waitName = $derived(childWait
		? [`Waiting for ${childWait.label}${waitMore}`, waitChecksBack(childWait.until, $minuteClock)].filter(Boolean).join(', ')
		: null);
</script>

<span class="child-actions">
	<button
		class="spawn-chip"
		type="button"
		onclick={(event) => {
			event.stopPropagation();
			onToggle();
		}}
		aria-expanded={expanded}
		aria-label={waitName ?? (hasLeft
			? `${childActivityLabel()} subagent, ${node.child?.progress.open} ${node.child?.progress.open === 1 ? 'subtask' : 'subtasks'} left`
			: `${childActivityLabel()} subagent, no open subtasks`)}
		title={waitName ?? `${childActivityLabel()} subagent · expand subtasks`}
	>
		{#if childWait}
			<ThreadWaitingMark size={12} />
			<span class="child-wait"><span class="child-wait-label">{childWait.label}{waitMore}</span>{#if waitBack(childWait.until, $minuteClock)}<span class="child-wait-fixed">{' · '}{waitBack(childWait.until, $minuteClock)}</span>{/if}</span>
		{:else}
			<span class="child-dot {childActivity()}" aria-hidden="true"></span>
		{/if}
		{#if hasLeft && !childWait}
			<span>{node.child?.progress.open} left</span>
		{/if}
	</button>
	<button
		class="child-open"
		type="button"
		data-plan-child-open={node.id}
		aria-label="Open the agent working on {node.title} beside this chat"
		title="Open the agent beside this chat"
		onclick={(event) => {
			// A sibling of the disclosure button, not a descendant — but
			// stop here anyway so navigation can never also toggle.
			event.stopPropagation();
			onOpen();
		}}
	>
		<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5 5-5 5" /></svg>
	</button>
</span>

<style>
	/* A child's wait chip is a sentence, not a count: it takes the row's second line so the title keeps its width. */
	/* The element type outranks PlanTree's own scoped `.tree-row` nowrap, whichever stylesheet loads last. */
	:global(div.tree-row:has(.child-wait)) { flex-wrap: wrap; row-gap: 0; }
	.child-actions { display: inline-flex; flex: none; align-items: center; gap: var(--space-1); }
	/* On the wait chip's own line the sentence takes the width and the open button stays at the trailing edge. */
	.child-actions:has(.child-wait) { flex: 1 1 auto; min-width: 0; }
	.child-actions:has(.child-wait) .spawn-chip { flex: 1 1 auto; min-width: 0; margin-right: auto; text-align: left; }
	.spawn-chip {
		display: inline-flex; align-items: center; gap: 6px; border: 0; border-radius: var(--radius-pill);
		background: transparent; color: var(--text-tertiary); padding: var(--space-1);
		font-size: var(--text-caption); font-variant-numeric: tabular-nums; cursor: pointer;
	}
	.child-dot {
		display: inline-block; width: 7px; height: 7px; border-radius: var(--radius-circle);
		background: var(--text-tertiary);
	}
	.child-dot.active { background: var(--blue); animation: child-pulse 1.8s ease-in-out infinite; }
	.child-dot.blocked { background: var(--red); }
	/* A child awaiting review is not idle grey, and is not motion either. */
	.child-dot.review { background: var(--text-secondary); }
	.child-dot.human-review { background: var(--amber); }
	/* Only the label yields; the time never wraps. */
	.child-wait { display: flex; min-width: 0; white-space: nowrap; text-align: left; }
	.child-wait-fixed { flex: none; white-space: pre; }
	.child-wait-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.child-open {
		display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px;
		border: 0; border-radius: var(--radius-xs); background: transparent;
		color: var(--text-tertiary); cursor: pointer;
		/* The glyph, not the hit area, ends on the row's trailing column. */
		margin-right: -4px;
	}
	.child-open svg {
		width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8;
		stroke-linecap: round; stroke-linejoin: round;
	}
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last thing tapped. */
	@media (hover: hover) {
		.spawn-chip:hover, .child-open:hover { color: var(--text-primary); background: var(--bg-hover); }
	}
	.spawn-chip:focus-visible, .child-open:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	@keyframes child-pulse { 50% { opacity: 0.38; transform: scale(0.82); } }
	@media (prefers-reduced-motion: reduce) {
		.child-dot.active { animation: none; }
	}
	@media (max-width: 620px) {
		.spawn-chip, .child-open { min-height: 44px; }
		.child-open { min-width: 44px; }
	}
</style>
