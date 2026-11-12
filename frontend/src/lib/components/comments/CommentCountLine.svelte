<!--
	A line that ends in the comments and suggestions waiting for the person: the needs-you dot, then the count
	in the accent colour. The count is what the person must notice, so it is never the part cut off. The lead
	(a row's own subtext) truncates first, down to a few characters; if the dot and count still don't fit, they
	wrap onto their own line. A count wider than a whole line wraps between its words. The dot is what
	separates the count from the lead, so a wrapped count starts at the line's edge with nothing before it.
	The plan row (PlanRowFace) and the ask card in chat (PersonStepRow) both use this, so the rule lives in
	one place.
-->
<script lang="ts">
	import type { Snippet } from 'svelte';
	import { feedbackLine } from '$lib/api/messageCards';

	let { counts, dot = false, lead }: {
		counts: { comments?: number; suggestions?: number };
		/** The needs-you dot: the item is the person's to act on. */
		dot?: boolean;
		/** The row's own subtext, ahead of the count. */
		lead?: Snippet;
	} = $props();
</script>

<!-- One line of markup: whitespace between the parts would become text in the line. -->
<span class="count-line">{#if lead}<span class="count-lead">{@render lead()}</span><span class="count-sep">{' · '}</span>{/if}<span class="comment-count">{#if dot}<span class="comment-dot" aria-hidden="true"></span>{/if}{feedbackLine(counts)}</span></span>

<style>
	/* The column gap spaces the count from the lead only while they share a line. */
	.count-line { position: relative; display: flex; flex-wrap: wrap; align-items: baseline; column-gap: var(--space-2); min-width: 0; max-width: 100%; }
	/* The lead's basis is its floor: below it, the count moves to its own line rather than cut the lead to nothing. */
	.count-lead {
		flex: 1 1 4em; max-width: max-content; min-width: 0;
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	/* Read, not drawn: the dot separates the parts on screen; the text and a screen reader keep the separator. */
	.count-sep { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: pre; }
	.comment-count { flex: 0 1 auto; min-width: 0; color: var(--accent); white-space: normal; }
	/* The sidebar's needs-you dot (ThreadPlanStateMark), set in the text line. */
	.comment-dot {
		display: inline-block; width: 8px; height: 8px; margin-right: var(--space-1);
		border-radius: var(--radius-circle); background: var(--amber); vertical-align: 0.05em;
	}
</style>
