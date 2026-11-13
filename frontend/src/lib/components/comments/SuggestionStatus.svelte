<script lang="ts">
	import type { DocComment } from '$lib/stores/docComments';
	let { authors, busy, shownBusy, skipped, error, acceptAll }: {
		authors: [string, DocComment[]][];
		busy: string | null;
		shownBusy: string | null;
		skipped: { id: string; reason: string }[];
		error: string;
		acceptAll: (author: string, id: string) => void;
	} = $props();
</script>

<div class="suggestion-status">
	{#each authors as [author, suggestions] (author)}
		<button type="button" aria-busy={shownBusy === author} disabled={busy !== null} onclick={() => acceptAll(author, suggestions[0].id)}>{shownBusy === author ? 'Accepting…' : `Accept all from ${author}`}</button>
	{/each}
	{#if skipped.length}<details><summary>{skipped.length} skipped</summary>{#each skipped as item (item.id)}<p>{item.reason}</p>{/each}</details>{/if}
</div>
{#if error}<div class="suggestion-error" role="alert">{error}</div>{/if}

<style>
	.suggestion-status { display: flex; flex-wrap: wrap; gap: calc(var(--space-3) / 2); align-items: center; }
	.suggestion-status button { background: none; border: none; padding: 0; font: inherit; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent-text); cursor: pointer; }
	.suggestion-status button:not(:disabled):hover { text-decoration: underline; }
	@media (pointer: coarse) { .suggestion-status button { min-height: var(--touch-target); } }
	.suggestion-status button:disabled { opacity: 0.55; cursor: not-allowed; }
	.suggestion-status button:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-wash); border-radius: var(--radius-xs); padding: calc(var(--space-1) / 2) calc(var(--space-3) / 2); margin: calc(var(--space-1) / -2) calc(var(--space-3) / -2); opacity: 1; }
	.suggestion-error { font-size: var(--text-caption); color: var(--red-text); }
	.suggestion-status details { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.suggestion-status p { margin: var(--space-1) 0; }
</style>
