<script lang="ts">
	let { dotOnly = false, failedQuote = null }: { dotOnly?: boolean; failedQuote?: string | null } = $props();
</script>

<span class="ct-new" class:dot-only={dotOnly} class:failed={failedQuote !== null} role={failedQuote !== null ? 'img' : undefined} aria-label={failedQuote !== null ? `Your reply on ${failedQuote} wasn’t delivered.` : undefined}><span aria-hidden="true" class="ct-new-dot"></span><span class:visually-hidden={dotOnly || failedQuote !== null}>New</span></span>

<style>
	.ct-new { display: inline-flex; align-items: baseline; gap: var(--space-1); font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent-text); }
	.ct-new-dot { width: var(--space-1); height: var(--space-1); border-radius: var(--radius-circle); flex-shrink: 0; align-self: center; background: var(--accent); }
	.failed .ct-new-dot { background: var(--red); }
	.dot-only { gap: 0; }
	.visually-hidden { position: absolute; inline-size: 0; block-size: 0; margin: 0; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
</style>
