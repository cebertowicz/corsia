<!--
	The show/collapse control of rows a level gathers away: its finished work (paged
	open) and its Scheduled row (all at once, `single`). One component, so the two
	cannot drift. Single mode keeps one button, so focus stays put while it flips
	between "Show N" and "Collapse".
-->
<script lang="ts">
	let { shown, total, page = total, noun, single = false, pending = false, controls, onShow, onCollapse }: {
		/** How many of the gathered rows are open now. */
		shown: number;
		total: number;
		/** Rows one press opens. */
		page?: number;
		/** What the rows are called in the accessible name ("finished steps"). */
		noun: string;
		single?: boolean;
		/** A cold history page is loading: keep that state on the Show control, not as a line in the tree. The spinner matches the shared primitive (`$lib/components/ui/Button.svelte:41`). */
		pending?: boolean;
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

<span class="row-actions" class:expanded>
	{#if single}
		<button
			class="details-toggle" type="button" aria-expanded={expanded} aria-controls={owned}
			aria-label={expanded ? `Collapse ${noun}` : `Show ${total} ${noun}`} aria-busy={pending || undefined} disabled={pending}
			onclick={expanded ? onCollapse : onShow}
		>{#if pending}<span class="spinner" aria-hidden="true"></span>{/if}{expanded ? 'Collapse' : `Show ${total}`}</button>
	{:else}
		{#if remaining > 0}
			<button
				class="details-toggle" type="button" aria-expanded={expanded} aria-controls={owned}
				aria-label={`Show ${step} of ${remaining} ${noun}`} aria-busy={pending || undefined} disabled={pending} onclick={onShow}
			>{#if pending}<span class="spinner" aria-hidden="true"></span>{/if}{expanded ? `Show ${step} more` : `Show ${step}`}</button>
		{/if}
		{#if expanded}
			<button class="details-toggle" type="button" aria-expanded="true" aria-controls={owned} onclick={onCollapse}>Collapse</button>
		{/if}
	{/if}
</span>

<style>
	.row-actions { display: inline-flex; flex: none; flex-wrap: nowrap; justify-content: flex-end; align-items: center; gap: var(--space-2); margin-left: auto; }
	/* The control line arrives as the fold opens, including when a narrow row moves it below its label. */
	.row-actions.expanded { animation: fold-actions-in var(--dur-standard) var(--ease-entrance); }
	@keyframes fold-actions-in {
		from { opacity: 0; transform: translateY(calc(var(--space-1) * -1)); }
		to { opacity: 1; transform: translateY(0); }
	}
	.details-toggle { display: inline-flex; align-items: center; gap: var(--space-1); flex: none; min-height: 28px; padding: var(--space-1) var(--space-2); border: 0; border-radius: var(--radius-xs); background: transparent; color: var(--text-tertiary); font: inherit; font-size: var(--text-caption); cursor: pointer; }
	.details-toggle:disabled { cursor: wait; }
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last thing tapped. */
	@media (hover: hover) { .details-toggle:hover:not(:disabled) { color: var(--text-primary); background: var(--bg-hover); } }
	.details-toggle:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
	/* Same geometry and token-driven spin as the shared Button primitive above. */
	.spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid currentColor; border-right-color: transparent; border-radius: var(--radius-circle); animation: spin var(--dur-hero-plus) linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .spinner, .row-actions.expanded { animation: none; } }
	@media (max-width: 620px) { .details-toggle { min-height: 44px; } }
</style>
