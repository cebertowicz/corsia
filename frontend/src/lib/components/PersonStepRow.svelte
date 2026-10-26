<script lang="ts">
	/**
	 * A person task in the transcript (corsia#687): the "Needs your review" tile itself (`NeedsYouTile`),
	 * with the plan row's needs-you mark as its lead and the task's action ("Do →") where the tile has
	 * its chevron. Do → opens what the plan row opens: the task's instructions document, else its
	 * details in the plan.
	 *
	 * Settled, the chip becomes the EventChip receipt in place. Several waiting tasks in one Focus
	 * stretch read as one line, the first task's title and "+N more", and Do → opens that first task;
	 * as each settles, it leaves the line and shows its own receipt.
	 */
	import { onMount, tick } from 'svelte';
	import { slide } from 'svelte/transition';
	import type { UpdateCard } from '$lib/api/threadUpdates';
	import { doStep, moreLabel, stepState } from '$lib/personStepChip';
	import { planQuery } from '$lib/stores/planQuery.svelte';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import EventChip from './EventChip.svelte';
	import NeedsYouTile from './NeedsYouTile.svelte';
	import OutcomeTime from './OutcomeTime.svelte';
	import PlanStateIcon from './plans/PlanStateIcon.svelte';

	let { card, group = undefined }: { card: UpdateCard; group?: UpdateCard[] } = $props();

	const members = $derived(group && group.length > 1 ? group : [card]);
	const query = planQuery(() => ({ kind: 'thread', id: card.thread_id }));
	const nodes = $derived(new Map((query.payload?.plans.flatMap((plan) => plan.nodes) ?? []).map((node) => [node.id, node])));
	const states = $derived(members.map((member) => stepState(member, member.node_id ? nodes.get(member.node_id) : undefined)));
	const waiting = $derived(members.filter((_, index) => states[index] === 'open'));
	const settled = $derived(members.flatMap((member, index) => (states[index] === 'open' ? [] : [{ member, state: states[index] }])));
	// The line: the first waiting task's title; with more waiting, "+N more", and the count falls as they settle.
	const title = $derived((waiting[0] ?? card).title || 'A task for you');
	const more = $derived(waiting.length > 1 ? moreLabel(waiting.length) : null);
	const single = $derived(members.length === 1);
	const motion = $derived($prefersReducedMotion ? 0 : 200);

	function receipt(member: UpdateCard, state: 'done' | 'closed') {
		return {
			id: `person-step-${member.id}`,
			role: 'system',
			content: '',
			metadata: {
				event_type: state === 'closed' ? 'person_step.wont_do' : 'person_step.done',
				// EventChip owns the outcome word; the summary is the task alone.
				summary: member.title || 'A task for you',
			},
		};
	}

	function press(event: MouseEvent): void {
		// The chat column takes every click as "the chat is active" and would put the chat back over the document just opened (`AttachmentRow` stops it the same way).
		event.stopPropagation();
		if (waiting[0]) void doStep(waiting[0]);
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

{#snippet mark()}
	<span class="lead"><PlanStateIcon state="needs-you" tone="" label="Needs you"><span aria-hidden="true">!</span></PlanStateIcon></span>
{/snippet}
{#snippet moreMark()}<span>· {more}</span>{/snippet}
{#snippet stateLine()}
	<span class="state ask"><span class="dot" aria-hidden="true"></span>Needs you</span>
	<span class="sep" aria-hidden="true">·</span>
	<OutcomeTime value={waiting[0]?.created_at ?? card.created_at} compact />
{/snippet}
{#snippet doMark()}<span class="act" aria-hidden="true">Do →</span>{/snippet}
{#snippet tile()}
	<NeedsYouTile
		needsYou
		{title}
		lead={mark}
		aside={more ? moreMark : undefined}
		follow
		ctx={stateLine}
		trailing={doMark}
		data-step-chip
		aria-label={`Do: ${title}${more ? `, ${waiting.length - 1} more` : ''}, Needs you`}
		onclick={press}
	/>
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
	/* The tile's lead slot is 36 px, as an artifact's type tile; the mark sits centred in it. */
	.lead { display: flex; flex: 0 0 auto; align-items: center; justify-content: center; width: 36px; height: 36px; }
	.sep { flex: 0 0 auto; color: var(--text-tertiary); }
	/* Where the tile has a chevron: the step's action, in its own word. */
	.act { flex: 0 0 auto; padding-right: var(--space-2); color: var(--accent); font-size: var(--text-footnote); font-weight: var(--fw-medium); }
	@media (prefers-reduced-motion: reduce) {
		.slot.ready, .slot.ready .slot-layer { transition: none; }
	}
</style>
