<!--
	A plan item's live step: what its running work is doing, and for how long.

	Mounted only while the row has a running Turn. It follows that thread's
	Turn frames for as long as it shows, so a worker the user is not viewing
	still reports its current step. A new phrase waits out the current one's
	minimum hold, then crossfades in; the line never changes height, and it is
	not a live region, so a phrase change is not announced.
-->
<script lang="ts">
	import { untrack } from 'svelte';
	import { watchThread } from '$lib/ws';
	import type { OperatorNode } from '$lib/api/plans';
	import { openMilliseconds, openTimes, requestOpenTime } from '$lib/openTime';
	import { createPhraseSwap } from './phraseSwap.svelte';
	import { FADE_MS, liveStepClock, liveStepLabel, stepOpenQuery, type LiveStep } from './planLiveStep';

	let { step, node }: { step: LiveStep; node: OperatorNode } = $props();

	const threadId = $derived(step.threadId);
	$effect(() => watchThread(threadId));

	const swap = createPhraseSwap(() => step.phrase, () => step.between);

	// The step's time is the server's open time, not this Turn's: it carries on across Turns, workers and waits.
	// It is read again when the plan's step changes or a new Turn starts, and counts on locally between.
	const query = $derived(stepOpenQuery(node, step.threadId));
	const asked = $derived(JSON.stringify([query, node.revision, step.startedAt]));
	$effect(() => {
		void asked;
		const wanted = untrack(() => query);
		if (wanted) requestOpenTime([wanted]);
	});
	const label = $derived(liveStepLabel(
		{ ...step, phrase: swap.shown },
		query ? openMilliseconds($openTimes, query.key, $liveStepClock) : null,
	));
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
