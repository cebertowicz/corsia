<script lang="ts">
	/**
	 * The new sign: "New" in the accent text colour, the same on a card and on its highlight. On a highlight it is
	 * pinned over the text (`highlightDot`), on the page's own colour so the line under it does not show through. A reply
	 * that wasn't delivered is a red dot instead.
	 */
	let { pinned = false, dot = false, failedQuote = null, name = 'New reply', mark = 'word' }: { pinned?: boolean; /** Lead the word with the accent dot. */ dot?: boolean; failedQuote?: string | null; /** The pinned sign's accessible name: what is new on its thread. */ name?: 'New comment' | 'New reply'; /** Prototype: the accent dot alone. */ mark?: 'word' | 'dot' } = $props();
	const failed = $derived(failedQuote !== null);
</script>

<span class="ct-new" class:pinned class:failed class:dotonly={mark === 'dot'} role={pinned || failed ? 'img' : undefined} aria-label={failed ? `Your reply on ${failedQuote} wasn’t delivered.` : pinned ? name : undefined}>{#if dot || failed || mark === 'dot'}<span aria-hidden="true" class="ct-new-dot"></span>{/if}{#if !failed && mark === 'word'}<span aria-hidden={pinned ? 'true' : undefined}>New</span>{/if}</span>

<style>
	.ct-new { display: inline-flex; align-items: baseline; gap: var(--space-1); font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent-text); }
	.ct-new-dot { width: var(--space-1); height: var(--space-1); border-radius: var(--radius-circle); flex-shrink: 0; align-self: center; background: var(--accent); }
	.failed .ct-new-dot { background: var(--red); }
	.pinned.dotonly { padding: 0; background: none; line-height: 0; }
	/* Prototype: 6px fill with a 1px ring in the page colour, so it reads as a mark and keeps off the underline above it. */
	.dotonly .ct-new-dot { width: 6px; height: 6px; box-shadow: 0 0 0 1px var(--bg-primary); }
	@media (forced-colors: active) { .ct-new-dot { background: CanvasText; } }
	.pinned:not(.failed):not(.dotonly) { padding: 0 var(--space-1); border-radius: var(--radius-2xs); background: var(--bg-primary); line-height: 1.5; }
</style>
