<script lang="ts">
	// A document's pending-send status and Send control, shown in its status row and, for the same batch,
	// in the bar of an image viewer over it (DCOL-FR13.12).
	import { Button } from '$lib/components/ui';
	import type { DocFeedback } from '$lib/docFeedback.svelte';

	let { feedback, compact = false }: {
		feedback: DocFeedback;
		/** A narrow bar: the Send control alone, its full wording read out rather than shown. */
		compact?: boolean;
	} = $props();
	const shortLabel = $derived(feedback.sendingGrace ? 'Send now' : `Send ${feedback.pendingCount}`);
</script>

{#if feedback.pendingCount > 0}
	{#if compact}
		<span class="sending-now compact">
			<span class="sr-only" role="status">{feedback.sendingGrace ? feedback.graceText : ''}</span>
			<Button size="sm" onclick={feedback.send} loading={feedback.sending} ariaLabel={feedback.sendingGrace ? `${feedback.graceText}. Send now` : feedback.sendLabel}>{shortLabel}</Button>
		</span>
	{:else if feedback.sendingGrace}
		<span class="sending-now">
			<span class="sending-grace" role="status" title={feedback.graceText}>Sending to <span class="grace-owner">{feedback.ownerName ?? 'the owner'}</span> in {feedback.graceMinutes} min</span>
			<Button size="sm" onclick={feedback.send} loading={feedback.sending}>Send now</Button>
		</span>
	{:else}
		<Button size="sm" onclick={feedback.send} loading={feedback.sending}>{feedback.sendLabel}</Button>
	{/if}
{/if}
<span class="undelivered" class:idle={feedback.undeliveredCount === 0}>
	<span role="status">{feedback.undeliveredCount > 0 ? feedback.undeliveredSummary : ''}</span>
	{#if feedback.undeliveredCount > 0}
		<Button size="sm" variant="secondary" onclick={feedback.retry} loading={feedback.retrying}>Retry</Button>
	{/if}
</span>

<style>
	.sending-grace {
		display: inline-flex;
		gap: 0.25em;
		min-width: 0;
		overflow: hidden;
		white-space: nowrap;
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}
	/* Only the owner name gives way; "Sending to" and the countdown stay whole. */
	.grace-owner { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.undelivered {
		display: inline-flex;
		flex-wrap: wrap;
		min-width: 0;
		max-width: 100%;
		align-items: center;
		gap: 4px;
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}
	.undelivered > span { flex: 1 1 12rem; min-width: 0; overflow-wrap: anywhere; }
	.undelivered :global(> .btn) { flex: none; min-height: 44px; }
	.undelivered :global(> .btn::after) { inset: 0; }
	/* Keep the live region mounted but out of the row's flow until it has text. */
	.undelivered.idle { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
	.compact { display: inline-flex; flex: none; }
	@media (max-width: 768px) {
		.compact :global(> .btn) { min-height: 44px; }
	}
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
