<script lang="ts">
	/**
	 * A message held for the person's approval, drawn in the decision drawer.
	 *
	 * The text is shown exactly as it would be sent (line breaks kept, never editable): approving
	 * covers these bytes and no others. There is no option list, no recommendation and no free-text
	 * choice. Approve once and Decline hand off to `attentionHeld`, so the dock advances at once and
	 * a failed write returns the reply here with its reason.
	 */
	import Button from '$lib/components/ui/Button.svelte';
	import type { HeldReplyDetail } from '$lib/api/attentionQueue';
	import { answers } from '$lib/stores/attentionAnswerState';
	import { decideHeld } from '$lib/stores/attentionHeld.svelte';

	let {
		held,
		writesDisabled = false,
		onSkip,
	}: {
		held: HeldReplyDetail;
		/** True while the surface is offline or showing last-known data. */
		writesDisabled?: boolean;
		/** Present only when another item waits: move on, keeping this one open. */
		onSkip?: () => void;
	} = $props();

	const headingId = $derived(`held-${held.id}`);
	const failure = $derived.by(() => {
		const answer = $answers[held.id];
		return answer?.state === 'failed' ? answer : null;
	});
	const locked = $derived(writesDisabled || failure?.kind === 'unauthorized');
	const problem = $derived(failure?.message ?? held.error);

	let well = $state<HTMLElement | undefined>();
	let lines = $state(0);
	let overflowing = $state(false);
	let atEnd = $state(true);

	/** How many lines the text takes as drawn at this width, and whether the well hides some. */
	function measure() {
		if (!well) return;
		const lineHeight = parseFloat(getComputedStyle(well).lineHeight);
		lines = lineHeight > 0 ? Math.round((well.scrollHeight - 2 * parseFloat(getComputedStyle(well).paddingTop)) / lineHeight) : 0;
		overflowing = well.scrollHeight > well.clientHeight + 1;
		atEnd = well.scrollTop + well.clientHeight >= well.scrollHeight - 1;
	}

	$effect(() => {
		held.text;
		measure();
		if (!well || typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(measure);
		observer.observe(well);
		return () => observer.disconnect();
	});
</script>

<section class="held-reply" aria-labelledby={headingId}>
	<header>
		<h3 id={headingId} tabindex="-1" data-drawer-autofocus>{held.title}</h3>
	</header>
	<div class="held-body">
		{#if problem}
			<p class="problem" role="alert">{problem}</p>
		{/if}
		<p class="explain">Approving sends the exact text below to that cloud provider.</p>
		<div class="well-frame" class:faded={overflowing && !atEnd}>
			<div class="well" bind:this={well} tabindex="0" role="textbox" aria-readonly="true" aria-multiline="true" aria-label="Exact text to send" onscroll={measure}>{held.text}</div>
		</div>
		{#if overflowing && !atEnd}
			<p class="cue">Scroll to read all {lines} lines</p>
		{/if}
	</div>
	<div class="held-actions">
		{#if writesDisabled}
			<p class="receipt">Approve once and Decline are paused until this reconnects.</p>
		{/if}
		<div class="buttons">
			{#if onSkip}
				<span class="skip"><Button variant="secondary" size="sm" ariaLabel="Skip to next item" onclick={onSkip}>Skip</Button></span>
			{/if}
			<Button variant="secondary" size="sm" disabled={locked} onclick={() => void decideHeld(held, 'decline')}>Decline</Button>
			<Button variant="primary" size="sm" disabled={locked} onclick={() => void decideHeld(held, 'approve')}>Approve once</Button>
		</div>
	</div>
</section>

<style>
	.held-reply { background: transparent; overflow: visible; }
	header { position: sticky; top: 0; z-index: 2; display: flex; padding: 0 0 var(--space-3); border-bottom: 1px solid var(--border-subtle); background: var(--bg-secondary); }
	h3 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); line-height: var(--lh-tight); }
	.held-body { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4) 0 0; }
	.explain { margin: 0; color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.problem { margin: 0; color: var(--red-text, var(--text-primary)); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.well-frame { position: relative; }
	/* The text is shown as sent: line breaks kept, long lines wrapped. */
	.well { max-height: 40vh; overflow-y: auto; padding: var(--space-2) var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-secondary); color: var(--text-primary); font-size: var(--text-footnote); line-height: var(--lh-normal); white-space: pre-wrap; overflow-wrap: anywhere; }
	.well:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.well-frame.faded::after { content: ''; position: absolute; inset: auto 1px 1px 1px; height: 2.5em; border-radius: 0 0 var(--radius-sm) var(--radius-sm); background: linear-gradient(to bottom, transparent, var(--bg-secondary)); pointer-events: none; }
	.cue { margin: 0; color: var(--text-tertiary); font-size: var(--text-caption); }
	.held-actions { position: sticky; bottom: 0; z-index: 2; display: flex; flex-direction: column; gap: var(--space-2); margin: var(--space-2) 0 0; padding: var(--space-3) 0 calc(max(var(--space-3), env(safe-area-inset-bottom, 0px)) + var(--kb-toolbar-inset, 0px)); border-top: 1px solid var(--border-subtle); background: var(--bg-secondary); }
	.receipt { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.buttons { display: flex; align-items: center; justify-content: flex-end; gap: var(--space-2); }
	/* Skip sits at the left edge of the row, apart from the two decisions. */
	.skip { margin-right: auto; }
</style>
