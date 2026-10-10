<!--
	A plan item's live step: what its running work is doing, and for how long.

	Mounted only while the row has a running Turn. It follows that thread's
	Turn frames for as long as it shows, so a worker the user is not viewing
	still reports its current step. A new phrase waits out the current one's
	minimum hold, then crossfades in; the line never changes height, and it is
	not a live region, so a phrase change is not announced.
-->
<script lang="ts">
	import { watchThread } from '$lib/ws';
	import { createPhraseSwap } from './phraseSwap.svelte';
	import { FADE_MS, liveStepClock, liveStepLabel, type LiveStep } from './planLiveStep';

	let { step }: { step: LiveStep } = $props();

	const threadId = $derived(step.threadId);
	$effect(() => watchThread(threadId));

	const swap = createPhraseSwap(() => step.phrase, () => step.between);

	const label = $derived(liveStepLabel({ ...step, phrase: swap.shown }, $liveStepClock));
</script>

<span class="live-step" class:dimmed={swap.dimmed} class:fading={swap.fading} style="--fade-ms: {FADE_MS}ms" title={label}>{label}</span>

<style>
	.live-step {
		display: block; flex: 1 1 auto; min-width: 0; overflow: hidden;
		text-overflow: ellipsis; white-space: nowrap; transition: opacity var(--fade-ms) ease;
	}
	.live-step.dimmed { opacity: 0.7; }
	.live-step.fading { opacity: 0; }
	@media (prefers-reduced-motion: reduce) {
		.live-step { transition: none; }
	}
</style>
