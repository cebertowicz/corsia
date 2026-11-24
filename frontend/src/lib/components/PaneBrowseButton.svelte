<script lang="ts">
	let { kind, onclick }: { kind: 'documents' | 'browser'; onclick: () => void } = $props();
</script>

<button class="browse" type="button" {onclick}>
	{#if kind === 'documents'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></svg>
		Browse documents…
	{:else}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></svg>
		Open browser
	{/if}
</button>

<style>
	.browse {
		display: flex; align-items: center; gap: var(--space-2); width: 100%;
		padding: var(--space-2); border: none;
		background: none; cursor: pointer; font: inherit;
		font-size: var(--text-subhead); color: var(--text-tertiary); text-align: left;
	}
	.browse:hover { color: var(--text-secondary); }
	.browse svg { width: calc(var(--space-4) * 0.9375); height: calc(var(--space-4) * 0.9375); flex-shrink: 0; }
</style>
