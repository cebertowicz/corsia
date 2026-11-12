<script lang="ts">
	// A document's undelivered status and, for one held lane, its Send control: the document's lane in its header and
	// the image lane in the bar of an image viewer over it (DCOL-FR13.12). Each lane has this one sender.
	import { tick } from 'svelte';
	import { Button } from '$lib/components/ui';
	import type { DocFeedback, HeldLane } from '$lib/docFeedback.svelte';
	import { inlineSlot } from '$lib/slotMotion';

	let { feedback, compact = false, lane = null }: {
		feedback: DocFeedback;
		/** A narrow bar: the Send control alone, its full wording read out rather than shown. */
		compact?: boolean;
		/** The held lane this control sends; none where Send lives elsewhere, so only what was not delivered, and Retry, show here. */
		lane?: HeldLane | null;
	} = $props();
	const held = $derived(lane ? feedback.heldCount(lane) : 0);
	const undelivered = $derived(feedback.undeliveredCount(lane));
	const summary = $derived(feedback.undeliveredSummary(lane));
	// A narrow bar confirms only its own lane's send; the document header confirms any send of the document it shows, since the
	// document's canvas carries no delivery notice of its own.
	const sentHere = $derived(lane !== null && feedback.sent > 0 && (feedback.sentLane === lane || !compact));
	let sender = $state<HTMLSpanElement>(), notSent = $state<HTMLSpanElement>();

	async function send() {
		if (!lane) return;
		// The image Send disappears once its comments go, or Retry takes its place when they were not delivered. Keep keyboard
		// focus on the next step: Retry when it stands there, else the viewer's durable Comment control. Decide by where focus
		// is when the send settles, not by the Sent line, which can end before a slow settle does.
		const viewer = compact && lane === 'image-held' && sender?.contains(document.activeElement) ? sender.closest('.image-comments') : null;
		await feedback.sendLane(lane);
		await tick();
		if (viewer && (document.activeElement === null || document.activeElement === document.body)) {
			const retry = feedback.undeliveredCount(lane) > 0 ? notSent?.querySelector<HTMLButtonElement>('button') : null;
			(retry ?? viewer.querySelector<HTMLButtonElement>('[data-image-comment]'))?.focus();
		}
	}
</script>

<!-- Send arrives and leaves in one slot, never popping in the row (motion.md); after a send that went it says Sent
     for a moment first. Leaving, the slot keeps what it last showed; a send not delivered swaps it for Retry at once. -->
{#if lane && (held > 0 || sentHere)}
	<span bind:this={sender} class="sending-now" class:compact in:inlineSlot out:inlineSlot={{ instant: feedback.handedToRetry }}>
		{#if held === 0}
			<span class="sent">
				<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5" /></svg>
				Sent
			</span>
		{:else}
			<Button size="sm" onclick={() => void send()} loading={feedback.sendingLane === lane} ariaLabel={feedback.sendLabel(lane)} title={feedback.sendText(lane)} dataShort={`Send ${held > 99 ? '99+' : held}`}>{feedback.sendText(lane)}</Button>
		{/if}
	</span>
{/if}
<!-- In a narrow bar the summary is read out, not laid out: Retry stands in Send's place and carries it as its name. -->
<span bind:this={notSent} class="undelivered" class:idle={undelivered === 0} class:compact>
	<span role="status" class:sr-only={compact}>{undelivered > 0 ? summary : feedback.sent > 0 ? feedback.sentText : ''}</span>
	{#if lane && undelivered > 0}
		<Button size="sm" variant="secondary" onclick={() => void feedback.retry(lane)} loading={feedback.retryingLane === lane} disabled={feedback.retryingLane !== null}
			ariaLabel={compact ? `Retry sending. ${summary}` : undefined} title={compact ? summary : undefined}>
			{#if compact}<svg class="alert" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7.5v5.5M12 16.5h.01" /></svg>{/if}
			Retry
		</Button>
	{/if}
</span>

<style>
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
	.undelivered > :global(.btn) { flex: none; min-height: 44px; }
	.undelivered > :global(.btn)::after { inset: 0; }
	.undelivered.compact { flex: none; flex-wrap: nowrap; }
	.alert { flex: none; color: var(--amber-text); }
	/* Keep the live region mounted but out of the row's flow until it has text. */
	.undelivered.idle { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
	.sending-now { display: inline-flex; flex: none; width: max-content; min-width: max-content; align-items: center; gap: 4px; white-space: nowrap; }
	/* A count is decisive text: the sender may move to the next line, never shrink its wording. */
	.sending-now > :global(.btn) { flex: none; min-width: max-content; width: max-content; }
	/* The press shrink would pull the touch padding's last pixels off the button before the tap lands. */
	.sending-now > :global(.btn:not(:disabled):active) { transform: none; background: var(--accent-wash); border-color: var(--accent-line); color: var(--accent-text, var(--accent)); }
	/* The document header and image bar each provide the size container for their compact Send label. */
	@container (max-width: 32.5rem) {
		.sending-now > :global(.btn) { font-size: 0; }
		.sending-now > :global(.btn)::before { content: attr(data-short); font-size: var(--text-footnote); }
		/* Sending swaps the count for the spinner in place, so the tight row never gains width mid-send and never wraps or moves the document. */
		.sending-now > :global(.btn.loading)::before { opacity: 0; }
		.sending-now > :global(.btn.loading) > :global(.spinner) { position: absolute; inset: 0; margin: auto; }
	}
	/* The image viewer's narrow bar, by its own width at any window size (a split beside chat or a phone), takes the same short
	   wording, so Back, the count and Send share one row. Same threshold as the bar's Back and count (ImageLightbox, ImageCommentLightbox). */
	@container image-bar (width < 600px) {
		.sending-now.compact > :global(.btn) { font-size: 0; }
		.sending-now.compact > :global(.btn)::before { content: attr(data-short); font-size: var(--text-footnote); }
		.sending-now.compact > :global(.btn.loading)::before { opacity: 0; }
		.sending-now.compact > :global(.btn.loading) > :global(.spinner) { position: absolute; inset: 0; margin: auto; }
	}
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
	@container doc-canvas (max-width: 37.5rem) {
		/* A narrow document canvas can occur beside chat on desktop as well as on a phone. */
		/* Send is already wider than the touch target and its own height meets it, so its hit-area pseudo-element
		   need not extend the scroll box past the full count label. */
		.sending-now > :global(.btn), .sending-now > .sent { min-height: var(--touch-target); }
		.sending-now > :global(.btn)::after { inset: 0; }
	}
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
