<!--
	The show/collapse control of rows a level gathers away: its finished work (paged
	open) and its Scheduled row (all at once, `single`). One component, so the two
	cannot drift. Single mode keeps one button, so focus stays put while it flips
	between "Show N" and "Collapse".
-->
<script lang="ts">
	let { shown, total, page = total, noun, single = false, controls, onShow, onCollapse }: {
		/** How many of the gathered rows are open now. */
		shown: number;
		total: number;
		/** Rows one press opens. */
		page?: number;
		/** What the rows are called in the accessible name ("finished steps"). */
		noun: string;
		single?: boolean;
		/** Space-separated ids of the rows open now, for `aria-controls`. */
		controls?: string;
		onShow: () => void;
		onCollapse: () => void;
	} = $props();

	const remaining = $derived(total - shown);
	const step = $derived(Math.min(page, remaining));
	const expanded = $derived(shown > 0);
	const owned = $derived(expanded ? controls : undefined);
</script>

<span class="row-actions">
	{#if single}
		<button
			class="details-toggle" type="button" aria-expanded={expanded} aria-controls={owned}
			aria-label={expanded ? `Collapse ${noun}` : `Show ${total} ${noun}`}
			onclick={expanded ? onCollapse : onShow}
		>{expanded ? 'Collapse' : `Show ${total}`}</button>
	{:else}
		{#if remaining > 0}
			<button
				class="details-toggle" type="button" aria-expanded={expanded} aria-controls={owned}
				aria-label={`Show ${step} of ${remaining} ${noun}`} onclick={onShow}
			>{expanded ? `Show ${step} more` : `Show ${step}`}</button>
		{/if}
		{#if expanded}
			<button class="details-toggle" type="button" aria-expanded="true" aria-controls={owned} onclick={onCollapse}>Collapse</button>
		{/if}
	{/if}
</span>

<style>
	.row-actions { display: inline-flex; flex: none; flex-wrap: nowrap; justify-content: flex-end; align-items: center; gap: var(--space-2); margin-left: auto; }
	.details-toggle {
		flex: none; min-height: 28px; padding: var(--space-1) var(--space-2);
		border: 0; border-radius: var(--radius-xs); background: transparent;
		color: var(--text-tertiary); font: inherit; font-size: var(--text-caption); cursor: pointer;
	}
	.details-toggle:hover { color: var(--text-primary); background: var(--bg-hover); }
	.details-toggle:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
	@media (max-width: 620px) {
		.details-toggle { min-height: 44px; }
	}
</style>
