<script lang="ts">
	/**
	 * The external-raise indicator (PRD-PLANS H4/H5).
	 *
	 * Two renderers, one substrate: the indicator and its queue slot are ours,
	 * the resolution surface belongs to the raising lane. This never offers an
	 * inline resolve and never a generic clear — it hands off.
	 */
	import { onMount, tick } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import IconButton from '$lib/components/ui/IconButton.svelte';
	import type { ExternalReviewDetail } from '$lib/api/attentionQueue';

	let {
		review,
		onClose = () => {},
		onSkip,
		onOpenDestination = () => {},
	}: {
		review: ExternalReviewDetail;
		onClose?: () => void;
		/** Present only when another queued item waits. */
		onSkip?: () => void;
		onOpenDestination?: (destination: { pane: string; id: string }) => void;
	} = $props();

	let heading = $state<HTMLElement | null>(null);

	// The dock remounts this row per item, which destroys whatever held focus
	// (the opener, or Skip). Land on the heading, as the decision drawer does,
	// so keyboard focus stays in the review and a screen reader reads its title.
	onMount(() => {
		let mounted = true;
		void tick().then(() => {
			if (mounted && heading?.isConnected) heading.focus();
		});
		return () => {
			mounted = false;
		};
	});

	// Whoever is not a person is named with what kind it is, in the words a person uses ("thread" is an agent here). The
	// spaces sit inside the string: whitespace at the edge of a template block is trimmed, which ran the words together.
	const KIND_WORD: Record<string, string> = { thread: 'agent', service: 'service' };
	const eyebrow = $derived.by(() => {
		const who = review.requester;
		if (!who) return 'Review';
		const kind = KIND_WORD[who.kind];
		return `Review · ${who.label ?? who.lane}${kind ? ` (${kind})` : ''}`;
	});

	// A destination with a path opens that file directly (`ChatPanel`), so the button names what opens: an item whose
	// producer marked it `purpose: 'send'` is an email draft's send review ("Open draft"), any other path is a document,
	// and a destination with no path keeps naming its pane.
	const openLabel = $derived.by(() => {
		const destination = review.destination;
		if (!destination?.path) return `Open in ${destination?.pane ?? ''}`;
		return review.purpose === 'send' ? 'Open draft' : 'Open document';
	});

	const headingId = $derived(`external-review-${review.review_key}`);
	const remaining = $derived(
		review.total_count > 0 ? `${review.pending_count} of ${review.total_count} left` : null,
	);

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.stopPropagation();
			onClose();
		}
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<section class="external-review" aria-labelledby={headingId} onkeydown={onKeydown}>
	<header>
		<div>
			<!-- A lane must never render as a person, so the kind is shown for
			     anything that is not a user. -->
			<span class="eyebrow">{eyebrow}</span>
			<h3 id={headingId} tabindex="-1" bind:this={heading}>{review.title}</h3>
			{#if remaining}<p class="remaining">{remaining}</p>{/if}
		</div>
		<IconButton ariaLabel="Collapse review" size="md" onclick={onClose}>
			<span aria-hidden="true">×</span>
		</IconButton>
	</header>
	<div class="actions" class:has-skip={!!onSkip}>
		{#if onSkip}
			<Button variant="secondary" size="sm" ariaLabel="Skip to next decision" onclick={onSkip}>
				Skip
			</Button>
		{/if}
		{#if review.destination}
			<Button
				variant="primary"
				size="sm"
				onclick={(event) => {
					// The chat column marks chat the active surface on any click inside it; bubbling there would flip a document just opened back to chat, which on a phone hides it (as `AttachmentRow` avoids).
					event.stopPropagation();
					onOpenDestination(review.destination!);
				}}
			>
				{openLabel}
			</Button>
		{:else}
			<p class="unavailable" role="status">
				This review has no destination recorded. Its source lane must repair it.
			</p>
		{/if}
	</div>
</section>

<style>
	.external-review { background: var(--bg-secondary); border: 1px solid var(--amber-line); border-radius: var(--radius-lg); box-shadow: var(--glass-elevation-2); overflow: hidden; }
	header { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-4) var(--space-5) var(--space-3); }
	.eyebrow { display: block; margin-bottom: var(--space-1); color: var(--amber); font-size: var(--text-caption); font-weight: var(--fw-semibold); letter-spacing: var(--ls-cap); text-transform: uppercase; }
	h3 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); line-height: var(--lh-tight); }
	.remaining { margin: var(--space-1) 0 0; color: var(--text-tertiary); font-size: var(--text-caption); font-variant-numeric: tabular-nums; }
	.actions { display: flex; justify-content: flex-end; gap: var(--space-2); padding: 0 var(--space-5) var(--space-4); }
	.unavailable { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	@media (max-width: 620px) {
		header, .actions { padding-left: var(--space-3); padding-right: var(--space-3); }
		.actions :global(button) { width: 100%; min-height: 44px; }
		.actions.has-skip :global(button) { width: auto; flex: none; }
	}
</style>
