<script lang="ts">
	import './comment-link.css';
	import { retryPlacement } from '$lib/stores/docComments';
	import type { CommentThreadData } from './CommentThread.svelte';

	let { comment }: { comment: CommentThreadData } = $props();

	// A placement normally completes in the same render turn as a new card. Do not flash a line that cannot be read,
	// or change the phone sheet's height during the composer-to-card swap.
	let showPlacement = $state(false);
	$effect(() => {
		if (comment.drawn || comment.placement !== 'placing') { showPlacement = false; return; }
		const timer = window.setTimeout(() => { showPlacement = true; }, 200);
		return () => window.clearTimeout(timer);
	});
</script>

{#if showPlacement}
	<p class="ct-placement" id="comment-placement-{comment.id}" data-comment-placement>Finding its place…</p>
{:else if !comment.drawn && comment.placement === 'not_placed'}
	<p class="ct-placement" id="comment-placement-{comment.id}" data-comment-placement>
		Couldn’t find its place
		<button class="ct-link" data-placement-retry onclick={(e) => { e.stopPropagation(); retryPlacement(comment.id); }}>Retry</button>
	</p>
{/if}

<style>
	.ct-placement { display: flex; align-items: center; gap: var(--space-2); margin: 0; font-size: var(--text-caption); color: var(--text-tertiary); }
</style>
