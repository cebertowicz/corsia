<script lang="ts">
	// A document's pending-send status and Send control, shown in its status row and, for the same batch,
	// in the bar of an image viewer over it (DCOL-FR13.12).
	import { Button } from '$lib/components/ui';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import { inlineSlot } from '$lib/slotMotion';

	let { feedback, compact = false }: {
		feedback: DocFeedback;
		/** A narrow bar: the Send control alone, its full wording read out rather than shown. */
		compact?: boolean;
	} = $props();
	const shortLabel = $derived(feedback.sendingGrace ? 'Send now' : `Send ${feedback.pendingCount}`);
</script>

<!-- Send arrives and leaves in one slot, never popping in the row (motion.md); after a send that went it says Sent
     for a moment first. Leaving, the slot keeps what it last showed; a send not delivered swaps it for Retry at once. -->
{#if feedback.pendingCount > 0 || feedback.sent > 0}
	<span class="sending-now" class:compact class:grace={!compact && feedback.sendingGrace} in:inlineSlot out:inlineSlot={{ instant: feedback.handedToRetry }}>
		{#if feedback.pendingCount === 0}
			<span class="sent">
				<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5" /></svg>
				Sent
			</span>
		{:else if compact}
			<span class="sr-only" role="status">{feedback.sendingGrace ? feedback.graceText : ''}</span>
			<Button size="sm" onclick={feedback.send} loading={feedback.sending} ariaLabel={feedback.sendingGrace ? `${feedback.graceText}. Send now` : feedback.sendLabel}>{shortLabel}</Button>
		{:else if feedback.sendingGrace}
			<span class="sending-grace" role="status" title={feedback.graceText}>Sending to <span class="grace-owner">{feedback.ownerName ?? 'the owner'}</span> in {feedback.graceMinutes} min</span>
			<Button size="sm" onclick={feedback.send} loading={feedback.sending}>Send now</Button>
		{:else}
			<Button size="sm" onclick={feedback.send} loading={feedback.sending}>{feedback.sendLabel}</Button>
		{/if}
	</span>
{/if}
<!-- In a narrow bar the summary is read out, not laid out: Retry stands in Send's place and carries it as its name. -->
<span class="undelivered" class:idle={feedback.undeliveredCount === 0} class:compact>
	<span role="status" class:sr-only={compact}>{feedback.undeliveredCount > 0 ? feedback.undeliveredSummary : feedback.sent > 0 ? feedback.sentText : ''}</span>
	{#if feedback.undeliveredCount > 0}
		<Button size="sm" variant="secondary" onclick={feedback.retry} loading={feedback.retrying}
			ariaLabel={compact ? `Retry sending. ${feedback.undeliveredSummary}` : undefined} title={compact ? feedback.undeliveredSummary : undefined}>
			{#if compact}<svg class="alert" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7.5v5.5M12 16.5h.01" /></svg>{/if}
			Retry
		</Button>
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
	.undelivered.compact { flex: none; flex-wrap: nowrap; }
	.alert { flex: none; color: var(--amber-text); }
	/* Keep the live region mounted but out of the row's flow until it has text. */
	.undelivered.idle { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
	.sending-now { display: inline-flex; flex: none; align-items: center; gap: 4px; white-space: nowrap; }
	.sent {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		height: 28px;
		padding: 0 10px;
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
		color: var(--green-text);
		animation: sent-in var(--dur-tight) var(--ease-snap);
	}
	/* Send turns into Sent in place: a crossfade, not a pop. */
	@keyframes sent-in { from { opacity: 0; } }
	@media (max-width: 768px) {
		.compact :global(> .btn), .compact > .sent { min-height: 44px; }
	}
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
