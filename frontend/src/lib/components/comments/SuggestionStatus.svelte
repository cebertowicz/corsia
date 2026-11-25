<script lang="ts">
	import type { DocComment } from '$lib/stores/docComments';
	let { authors, busy, shownBusy, skipped, error, acceptAll }: {
		authors: [string, DocComment[]][];
		busy: string | null;
		shownBusy: string | null;
		skipped: { id: string; reason: string }[];
		error: string;
		acceptAll: (author: string, id: string) => Promise<boolean>;
	} = $props();
	let choosing = $state(false);
	let selected = $state<string[]>([]);
	let submitting = $state(false);
	function open() {
		if (authors.length === 1) { void acceptAll(authors[0][0], authors[0][1][0].id); return; }
		selected = authors.map(([author]) => author);
		choosing = true;
	}
	async function confirm() {
		const chosen = authors.filter(([author]) => selected.includes(author)).map(([author, suggestions]) => [author, suggestions[0].id] as const);
		submitting = true;
		try {
			for (const [author, id] of chosen) if (!await acceptAll(author, id)) return;
			choosing = false;
		} finally { submitting = false; }
	}
</script>

<div class="suggestion-status">
	{#if authors.length}
		<button type="button" aria-busy={submitting || shownBusy !== null} disabled={busy !== null || submitting} onclick={open}>{submitting || shownBusy !== null ? 'Accepting…' : 'Accept suggestions'}</button>
		{#if choosing}
			<div class="suggestion-choices" role="group" aria-label="Choose whose suggestions to accept">
				{#each authors as [author, suggestions] (author)}
					<label><input type="checkbox" value={author} bind:group={selected} disabled={submitting} /> {author} · {suggestions.length}</label>
				{/each}
				<button type="button" disabled={!selected.length || submitting} onclick={confirm}>Accept selected</button>
				<button type="button" disabled={submitting} onclick={() => choosing = false}>Cancel</button>
			</div>
		{/if}
	{/if}
	{#if skipped.length}<details><summary>{skipped.length} skipped</summary>{#each skipped as item (item.id)}<p>{item.reason}</p>{/each}</details>{/if}
</div>
{#if error}<div class="suggestion-error" role="alert">{error}</div>{/if}

<style>
	.suggestion-status { display: flex; flex-wrap: wrap; gap: calc(var(--space-3) / 2); align-items: center; }
	.suggestion-status button { min-height: var(--space-6); background: none; border: none; padding: 0; font: inherit; font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--accent-text); cursor: pointer; }
	.suggestion-status button:not(:disabled):hover { text-decoration: underline; }
	.suggestion-status button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.suggestion-status button:disabled { opacity: 0.55; cursor: not-allowed; }
	.suggestion-status button[aria-busy="true"] { opacity: 1; }
	.suggestion-choices { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-2); width: 100%; }
	.suggestion-choices label { display: flex; align-items: center; gap: var(--space-2); min-height: var(--touch-target); }
	@media (pointer: coarse), (max-width: 48em) { .suggestion-status button { min-height: var(--touch-target); } }
	:global([data-adaptive-drawer]) .suggestion-status button { font-size: var(--text-subhead); }
	.suggestion-error { font-size: var(--text-footnote); color: var(--red-text); }
	.suggestion-status details { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.suggestion-status p { margin: var(--space-1) 0; }
</style>
