<script lang="ts">
	// One plan update in the transcript: the item's title as it read when the
	// outcome happened, and that outcome (AS17). No reason, evidence or note.
	import type { UpdateCard } from '$lib/api/threadUpdates';
	import { focusPlanNode } from '$lib/planNodeFocus';
	import { emitProductEvent } from '$lib/product_events';
	import AttachmentRow from './AttachmentRow.svelte';
	import OutcomeTime from './OutcomeTime.svelte';

	const OUTCOMES: Record<UpdateCard['kind'], string> = {
		done: 'Done',
		done_follow_up: 'Done, follow-up added',
		failed: 'Failed',
		closed: 'Closed',
		blocked: 'Blocked',
	};

	let { card, earlier = 0 }: { card: UpdateCard; earlier?: number } = $props();

	const title = $derived(card.title || 'Plan item');
	const outcome = $derived(OUTCOMES[card.kind] ?? card.kind);
	// A renamed or deleted item keeps its historical title as plain text.
	const opens = $derived(card.node_exists && Boolean(card.node_id));
	// The ask card is homed under its row in every round state, so it does not move when answered. Focus
	// tucks an answered block into a line instead of a row: the card then shows on its message.
	const ask = $derived(card.kind === 'blocked' ? card.ask ?? null : null);

	function openPlanNode(): void {
		emitProductEvent('update_card', 'plan_open');
		void focusPlanNode(card.thread_id, card.node_id);
	}
</script>

{#if ask}
	<!-- One fact, one row: an ask's blocked update and its artifact are the same hold (C4a), so the row is the artifact. -->
	<div class="update-card {card.kind} merged" data-update-id={card.id}>
		<div class="update-ask"><AttachmentRow card={ask} threadId={card.thread_id} task={title} time={card.created_at} /></div>
	</div>
{:else}
	<div class="update-card {card.kind}" data-update-id={card.id}>
		{#if opens}
			<button type="button" class="title" onclick={openPlanNode}>{title}</button>
		{:else}
			<span class="title">{title}</span>
		{/if}
		<span class="sep" aria-hidden="true">·</span>
		<span class="outcome">{outcome}</span>
		<span class="when"><OutcomeTime value={card.created_at} compact /></span>
		{#if earlier > 0}
			<span class="earlier">{earlier} earlier</span>
		{/if}
	</div>
{/if}

<style>
	.update-card {
		max-width: 760px;
		margin: 0 auto 12px;
		display: flex;
		align-items: baseline;
		gap: 6px;
		padding: 7px 10px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		background: var(--bg-secondary);
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}
	/* The merged row is flat: no box of its own, the artifact row inside it carries the look. */
	/* Full column width, so its chevron shares the right edge with every other row in the transcript. */
	.update-card.merged { display: block; padding: 0; border: 0; background: transparent; }
	.title {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--text-primary);
	}
	button.title {
		border: 0;
		padding: 0;
		background: transparent;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	button.title:hover { text-decoration: underline; }
	button.title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-2xs); }
	.sep, .earlier { color: var(--text-tertiary); }
	.outcome { flex-shrink: 0; }
	.earlier { flex-shrink: 0; margin-left: auto; }
	/* The same clock as every other transcript row: "9:41 AM" at the right edge, on one line; the title gives way first. */
	.when { margin-left: auto; flex: 0 0 auto; padding-left: 8px; white-space: nowrap; }
	.when :global(time) { white-space: nowrap; }
	.update-card.failed .outcome { color: var(--red-text); }
	.update-card.blocked .outcome { color: var(--amber); }
</style>
