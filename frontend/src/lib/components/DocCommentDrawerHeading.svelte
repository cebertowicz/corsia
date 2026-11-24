<script lang="ts">
	import type { Snippet } from 'svelte';
	import { IconButton } from './ui';
	import { menuPop } from '$lib/popoverMotion';
	import { COMMENT_FILTERS as FILTERS } from '$lib/docCommentDrawer';
	/** The comment list's heading in the sheet, the popover and the image drawer, and the resolved list's: its count, an image's Filter, and its close. */
	let { title = 'Comments', closeLabel = 'Close comments', count, meta, filtered, filter = $bindable('all'), filterOpen = $bindable(false), onClose }: {
		title?: string;
		closeLabel?: string;
		count: string;
		/** The phone sheet's Open and Resolved switch, set beside the title. It carries the counts, so the title drops its own. */
		meta?: Snippet;
		/** An image's drawer has its own Filter; a document's list does not. */
		filtered: boolean;
		filter?: 'all' | 'open' | 'resolved';
		filterOpen?: boolean;
		onClose: () => void;
	} = $props();
	const filterName = $derived(FILTERS.find(([key]) => key === filter)?.[1] ?? 'Open');
</script>

<!-- In the phone sheet the whole row is a handle as well as the grabber (AdaptiveDrawer); its buttons stay buttons. -->
<div class="drawer-heading" class:has-meta={!!meta} data-drawer-handle>{#if meta}<div class="heading-main"><h2>{title}</h2>{@render meta()}</div>{:else}<h2>{title}{#if count}{' '}<span>{count}</span>{/if}</h2>{/if}<div class="drawer-tools">{#if filtered}<div class="filter-menu">
	<button type="button" class="filter-button" data-drawer-filter aria-haspopup="menu" aria-expanded={filterOpen} aria-label="Filter comments: {filterName}" onclick={() => filterOpen = !filterOpen}>Filter <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button>
	{#if filterOpen}<div class="filter-popup" role="menu" aria-label="Filter comments" in:menuPop={{ origin: 'top right' }} out:menuPop={{ origin: 'top right' }}>{#each FILTERS as [key, name] (key)}<button type="button" role="menuitemradio" aria-checked={filter === key} onclick={() => { filter = key; filterOpen = false; }}><span class="filter-check" aria-hidden="true">{#if filter === key}<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10" /></svg>{/if}</span>{name}</button>{/each}</div>{/if}
</div>{/if}<IconButton ariaLabel={closeLabel} variant="ghost" size="sm" onclick={onClose}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg></IconButton></div></div>

<style>
	.drawer-heading { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); padding: 0 0 var(--space-1) var(--space-1); }
	.drawer-heading h2 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	/* With the switch as meta, the title and the switch share one row while both fit beside Close, and the switch wraps under the title (the two-row form) when they do not; Close stays at the top right. */
	.heading-main { display: flex; flex: 1 1 auto; flex-wrap: wrap; align-items: center; column-gap: var(--space-2); min-width: 0; }
	.has-meta h2 { display: flex; align-items: center; min-height: var(--touch-target); }
	.has-meta .drawer-tools { align-self: flex-start; min-height: var(--touch-target); }
	.drawer-heading h2 span { color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
	/* The compact menu keeps the filter choices out of the thread list. */
	.drawer-tools { display: inline-flex; align-items: center; gap: var(--space-1); margin-left: auto; }
	.filter-menu { position: relative; }
	.filter-button, .filter-popup button { min-height: var(--space-6); border: 0; border-radius: var(--radius-xs); background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); cursor: pointer; }
	.filter-button { display: inline-flex; align-items: center; gap: var(--space-1); padding: 0 var(--space-2); }
	.filter-popup { position: absolute; z-index: 2; right: 0; top: calc(100% + var(--space-1)); display: grid; min-width: 9rem; padding: var(--space-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-elevated); box-shadow: var(--shadow-chip); }
	.filter-popup button { display: flex; align-items: center; gap: var(--space-1); padding: 0 var(--space-2); text-align: left; }
	.filter-check { display: inline-flex; flex: none; width: var(--space-3); height: var(--space-3); }
	.filter-popup button[aria-checked="true"] { color: var(--accent-text); font-weight: var(--fw-semibold); }
	@media (pointer: coarse) { .drawer-heading :global(button), .filter-button, .filter-popup button { min-height: var(--touch-target); } .drawer-heading :global(.ico-btn) { min-width: var(--touch-target); } }
	@media (max-width: 48rem) {
		/* Keep the full touch target inside the drawer instead of extending it past the edge. */
		.drawer-heading :global(.ico-btn) { width: var(--touch-target); height: var(--touch-target); }
		.drawer-heading :global(.ico-btn::after) { inset: 0; }
	}
</style>
