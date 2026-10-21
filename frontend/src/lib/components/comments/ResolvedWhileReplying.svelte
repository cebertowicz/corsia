<script lang="ts">
	/**
	 * Said on a thread's card when it was resolved while a reply to it is still unsent: what Send will do (add the reply, leave
	 * the thread resolved, as the comments API does for any reply to a resolved thread), and the way to reopen it instead.
	 */
	let { readOnly = false, onReopen }: { readOnly?: boolean; onReopen?: () => void } = $props();
</script>

<p class="held" role="status" data-resolved-note>
	<svg class="check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5" /></svg>
	<span class="text">Resolved while you were replying. Send adds your reply; the thread stays resolved.</span>
	{#if !readOnly}<button type="button" class="reopen" onclick={(e) => { e.stopPropagation(); onReopen?.(); }}>Re-open</button>{/if}
</p>

<style>
	.held { margin: 0; font-size: var(--text-caption); line-height: var(--lh-snug); color: var(--text-secondary); overflow-wrap: anywhere; }
	.check { color: var(--green); vertical-align: -2px; margin-right: 4px; }
	.reopen { display: inline; padding: 0 0 0 4px; font: inherit; font-weight: var(--fw-semibold); color: var(--accent-text); background: none; border: none; cursor: pointer; }
	@media (pointer: coarse) { .reopen { min-height: calc(var(--space-8) - var(--space-1)); padding-inline: 8px; } }
	.reopen:hover { text-decoration: underline; }
	.reopen:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
</style>
