<script lang="ts">
	/**
	 * The new-reply sign: "New" in the accent text colour, the same on a card and on its highlight. On a highlight it is
	 * pinned over the text (`highlightDot`), on the page's own colour so the line under it does not show through. A reply
	 * that wasn't delivered is a red dot instead.
	 */
	let { pinned = false, dot = false, failedQuote = null }: { pinned?: boolean; /** Lead the word with the accent dot. */ dot?: boolean; failedQuote?: string | null } = $props();
	const failed = $derived(failedQuote !== null);
</script>

<span class="ct-new" class:pinned class:failed role={pinned || failed ? 'img' : undefined} aria-label={failed ? `Your reply on ${failedQuote} wasn’t delivered.` : pinned ? 'New reply' : undefined}>{#if dot || failed}<span aria-hidden="true" class="ct-new-dot"></span>{/if}{#if !failed}<span aria-hidden={pinned ? 'true' : undefined}>New</span>{/if}</span>

<style>
	.ct-new { display: inline-flex; align-items: baseline; gap: var(--space-1); font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent-text); }
	.ct-new-dot { width: var(--space-1); height: var(--space-1); border-radius: var(--radius-circle); flex-shrink: 0; align-self: center; background: var(--accent); }
	.failed .ct-new-dot { background: var(--red); }
	.pinned:not(.failed) { padding: 0 var(--space-1); border-radius: var(--radius-2xs); background: var(--bg-primary); line-height: 1.5; }
</style>
