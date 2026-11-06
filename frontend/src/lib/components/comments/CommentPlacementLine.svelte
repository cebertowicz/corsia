<script lang="ts">
	import './comment-link.css';
	import { retryPlacement } from '$lib/stores/docComments';
	import { commentLines } from '$lib/ws/commentTurn';
	import type { CommentThreadData } from './CommentThreadData';

	let { comment }: { comment: CommentThreadData } = $props();

	// A placement normally completes in the same render turn as a new card. Do not flash a line that cannot be read,
	// or change the phone sheet's height during the composer-to-card swap.
	let showPlacement = $state(false);
	$effect(() => {
		if (comment.drawn || comment.placement !== 'placing') { showPlacement = false; return; }
		const timer = window.setTimeout(() => { showPlacement = true; }, 200);
		return () => window.clearTimeout(timer);
	});
	// Agent writes waiting while the person holds this thread's writer turn; they post after the person sends or leaves.
	const waiting = $derived($commentLines[comment.id] ?? 0);
</script>

{#if showPlacement}
	<p class="ct-placement" id="comment-placement-{comment.id}" data-comment-placement>Finding its place…</p>
{:else if !comment.drawn && comment.placement === 'not_placed'}
	<p class="ct-placement" id="comment-placement-{comment.id}" data-comment-placement>
		Couldn’t find its place
		<button class="ct-link" data-placement-retry onclick={(e) => { e.stopPropagation(); retryPlacement(comment.id); }}>Retry</button>
	</p>
{/if}
{#if waiting > 0}
	<p class="ct-placement" role="status" data-comment-waiting>{waiting === 1 ? '1 agent waiting its turn.' : `${waiting} agents waiting their turn.`}</p>
{/if}

<style>
	.ct-placement { display: flex; align-items: center; gap: var(--space-2); margin: 0; font-size: var(--text-caption); color: var(--text-tertiary); }
</style>
