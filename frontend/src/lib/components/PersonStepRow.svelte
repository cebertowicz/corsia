<script lang="ts">
	/**
	 * A person step in the transcript (corsia#687): the "Needs your review" tile's own look and
	 * geometry, with the step's action ("Do →") where the tile has its chevron. Do → opens what the
	 * plan row opens: the step's instructions document, else its details in the plan.
	 *
	 * Settled, the chip becomes the EventChip receipt in place. Several ready steps in one Focus
	 * stretch read as one line; as each settles, it leaves the line and shows its own receipt.
	 */
	import { onMount, tick } from 'svelte';
	import { slide } from 'svelte/transition';
	import type { UpdateCard } from '$lib/api/threadUpdates';
	import { doFirstOf, doStep, groupLabel, stepState } from '$lib/personStepChip';
	import { planQuery } from '$lib/stores/planQuery.svelte';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import EventChip from './EventChip.svelte';
	import OutcomeTime from './OutcomeTime.svelte';

	let { card, group = undefined }: { card: UpdateCard; group?: UpdateCard[] } = $props();

	const members = $derived(group && group.length > 1 ? group : [card]);
	const query = planQuery(() => ({ kind: 'thread', id: card.thread_id }));
	const nodes = $derived(new Map((query.payload?.plans.flatMap((plan) => plan.nodes) ?? []).map((node) => [node.id, node])));
	const states = $derived(members.map((member) => stepState(member, member.node_id ? nodes.get(member.node_id) : undefined)));
	const waiting = $derived(members.filter((_, index) => states[index] === 'open'));
	const settled = $derived(members.flatMap((member, index) => (states[index] === 'open' ? [] : [{ member, state: states[index] }])));
	// The line: one step reads as its title; two or more as a count, and the count falls as steps settle.
	const title = $derived(waiting.length > 1 ? groupLabel(waiting.length) : ((waiting[0] ?? card).title || 'A step for you'));
	const single = $derived(members.length === 1);
	const motion = $derived($prefersReducedMotion ? 0 : 200);

	function receipt(member: UpdateCard, state: 'done' | 'closed') {
		return {
			id: `person-step-${member.id}`,
			role: 'system',
			content: '',
			metadata: {
				event_type: state === 'closed' ? 'person_step.wont_do' : 'person_step.done',
				// EventChip owns the outcome word; the summary is the step alone.
				summary: member.title || 'A step for you',
			},
		};
	}

	function press(event: MouseEvent): void {
		// The chat column takes every click as "the chat is active" and would put the chat back over the document just opened (`AttachmentRow` stops it the same way).
		event.stopPropagation();
		if (waiting.length > 1) void doFirstOf(waiting);
		else if (waiting[0]) void doStep(waiting[0]);
	}

	// One step: both faces stay mounted, so the row keeps its height until the next face is measured, then
	// changes once while the faces cross-fade (no jump). The face that is not shown is inert.
	const own = $derived(single ? states[0] : 'open');
	let chipLayer = $state<HTMLElement | null>(null);
	let receiptLayer = $state<HTMLElement | null>(null);
	let slotHeight = $state<number | null>(null);
	let slotReady = $state(false);

	function measureSlot(): void {
		const layer = own === 'open' ? chipLayer : receiptLayer;
		if (layer) slotHeight = Math.ceil(layer.getBoundingClientRect().height);
	}

	$effect(() => {
		own;
		void tick().then(measureSlot);
	});

	onMount(() => {
		if (!single) return;
		const observer = new ResizeObserver(measureSlot);
		if (chipLayer) observer.observe(chipLayer);
		if (receiptLayer) observer.observe(receiptLayer);
		void tick().then(() => {
			measureSlot();
			slotReady = true;
		});
		return () => observer.disconnect();
	});
</script>

{#snippet tile()}
	<button
		type="button"
		class="tile press-tint"
		data-step-chip
		title={title}
		aria-label={`Do: ${title}, Ready for you`}
		onclick={press}
	>
		<span class="ic" aria-hidden="true">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="3" /><path d="m8.5 12.3 2.4 2.4 4.6-4.9" /></svg>
		</span>
		<span class="text">
			<span class="head">
				<span class="title">{title}</span>
				<span class="when"><OutcomeTime value={waiting[0]?.created_at ?? card.created_at} compact /></span>
			</span>
			<span class="ctx"><span class="state"><span class="dot" aria-hidden="true"></span>Ready for you</span></span>
		</span>
		<span class="act" aria-hidden="true">Do →</span>
	</button>
{/snippet}

<div class="step-row" data-person-step={card.node_id} data-step-state={single ? own : waiting.length ? 'open' : 'settled'} data-step-count={members.length}>
	{#if single}
		<div
			class="slot"
			class:ready={slotReady}
			style:height={slotReady && slotHeight !== null ? `${slotHeight}px` : undefined}
		>
			<div bind:this={chipLayer} class="slot-layer" class:active={own === 'open'} aria-hidden={own !== 'open'} inert={own !== 'open'}>
				{@render tile()}
			</div>
			<div bind:this={receiptLayer} class="slot-layer" class:active={own !== 'open'} aria-hidden={own === 'open'} inert={own === 'open'}>
				<EventChip msg={receipt(card, own === 'closed' ? 'closed' : 'done')} />
			</div>
		</div>
	{:else}
		{#if waiting.length}
			<div class="stack-item" transition:slide={{ duration: motion }}>{@render tile()}</div>
		{/if}
		{#each settled as item (item.member.id)}
			<div class="stack-item" transition:slide={{ duration: motion }}>
				<EventChip msg={receipt(item.member, item.state === 'closed' ? 'closed' : 'done')} />
			</div>
		{/each}
	{/if}
</div>

<style>
	.step-row { max-width: 760px; margin: 0 auto var(--space-3); }
	.stack-item + .stack-item { margin-top: var(--space-2); }
	.slot { position: relative; max-width: 100%; }
	.slot.ready { transition: height var(--dur-standard) var(--ease-snap); }
	.slot:not(.ready) .slot-layer:not(.active) { display: none; }
	.slot.ready .slot-layer {
		position: absolute;
		inset: 0 auto auto 0;
		width: 100%;
		opacity: 0;
		pointer-events: none;
		transition: opacity var(--dur-standard) var(--ease-snap);
	}
	.slot.ready .slot-layer.active { opacity: 1; pointer-events: auto; }
	/* The "Needs your review" row's own values (AttachmentRow `.tile.row.needs-you`): the frosted card of an open ask. */
	.tile {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		min-width: 0;
		padding: var(--space-2) var(--space-2) var(--space-2) 2px;
		border: 1px solid var(--border);
		border-radius: var(--radius-xl);
		background: var(--glass-thin);
		box-shadow: var(--glass-rim), var(--shadow-chip);
		color: var(--text-primary);
		font: inherit;
		text-align: left;
		cursor: pointer;
		transition: background var(--dur-tight) var(--ease-snap);
	}
	@media (hover: hover) { .tile:hover { background: var(--bg-secondary); } }
	/* Inset, as the tile's: the row clips at the prose edge, so an outward ring would be cut off. */
	.tile:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (pointer: coarse) { .tile { min-height: 44px; } }
	.ic { display: flex; flex: 0 0 auto; align-items: center; justify-content: center; width: 36px; height: 36px; color: var(--text-tertiary); }
	.ic svg { width: 55%; height: 55%; }
	.text { display: flex; flex: 1 1 auto; flex-direction: column; min-width: 0; }
	.head { display: flex; align-items: baseline; gap: var(--space-2); min-width: 0; }
	.title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--text-callout);
		font-weight: var(--fw-semibold);
		line-height: var(--lh-tight);
	}
	.when { flex: 0 0 auto; font-size: var(--text-footnote); line-height: var(--lh-tight); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
	.ctx { display: flex; align-items: center; gap: var(--space-1); min-width: 0; font-size: var(--text-footnote); line-height: var(--lh-tight); color: var(--text-secondary); white-space: nowrap; }
	.state { display: inline-flex; align-items: center; gap: var(--space-1); color: var(--amber-text); }
	.dot { flex: 0 0 auto; width: 8px; height: 8px; border-radius: var(--radius-circle); background: var(--amber); }
	/* Where the tile has a chevron: the step's action, in its own word. */
	.act { flex: 0 0 auto; padding-right: var(--space-2); color: var(--accent); font-size: var(--text-footnote); font-weight: var(--fw-medium); }
	@media (prefers-reduced-motion: reduce) {
		.tile, .slot.ready, .slot.ready .slot-layer { transition: none; }
	}
</style>
