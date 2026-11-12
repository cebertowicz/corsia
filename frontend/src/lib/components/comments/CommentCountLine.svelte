<!--
	A line that ends in the comments and suggestions waiting for the person, in the accent text colour. Nothing
	on it is cut: the lead (a row's own subtext) keeps its full text, and when lead and count don't share a line
	the count wraps onto its own. A ' · ' separates the count from the lead while they share a line; on a line
	of its own the count starts at the edge, because the separator then sits left of the line and is clipped.
	`dot` puts the needs-you dot directly against the count; a line whose lead already shows that dot passes
	none, so a line never carries two. Every plan row (PlanRowFace) uses this, so the rule lives in one place.
-->
<script lang="ts">
	import type { Snippet } from 'svelte';
	import { feedbackLine } from '$lib/api/messageCards';

	let { counts, dot = false, lead }: {
		counts: { comments?: number; suggestions?: number };
		/** The needs-you dot, set against the count. */
		dot?: boolean;
		/** The row's own subtext, ahead of the count. */
		lead?: Snippet;
	} = $props();
</script>

<!-- One line of markup: whitespace between the parts would become text in the line. -->
<span class="count-line" class:has-lead={!!lead}><span class="count-flow">{#if lead}<span class="count-lead">{@render lead()}</span>{/if}<span class="comment-count">{#if lead}<span class="count-sep">{' · '}</span>{/if}<span class="count-text">{#if dot}<span class="comment-dot" aria-hidden="true"></span>{/if}{feedbackLine(counts)}</span></span></span></span>

<style>
	/*
	 * The separator trick: the flow is pulled left by the separator's width and the line clips what lies left of
	 * its edge. The lead is padded back by the same width, so it starts at the edge; a count that shares the lead's
	 * line shows its separator; a count that wraps starts its line, so its separator falls left of the edge, unseen.
	 */
	.count-line { --count-sep: 1.1em; display: block; min-width: 0; max-width: 100%; }
	.count-line.has-lead { overflow: hidden; }
	.count-flow { display: flex; flex-wrap: wrap; align-items: baseline; min-width: 0; }
	.has-lead > .count-flow { margin-left: calc(-1 * var(--count-sep)); }
	/* The lead's full text is its floor: it wraps within the line rather than lose a word to an ellipsis. */
	.count-lead { flex: 0 1 auto; min-width: 0; padding-left: var(--count-sep); white-space: normal; }
	.comment-count { display: inline-flex; flex: 0 1 auto; min-width: 0; align-items: baseline; }
	.count-sep { flex: 0 0 var(--count-sep); width: var(--count-sep); text-align: center; white-space: pre; }
	/* The text role of the accent: it holds 4.5:1 on the panel in both themes, as small text needs. */
	.count-text { min-width: 0; color: var(--accent-text); white-space: normal; }
	/* The sidebar's needs-you dot (ThreadPlanStateMark), set in the text line. */
	.comment-dot {
		display: inline-block; width: var(--space-2); height: var(--space-2); margin-right: var(--space-1);
		border-radius: var(--radius-circle); background: var(--amber); vertical-align: 0.05em;
	}
</style>
