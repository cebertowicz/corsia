<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		name?: string;
		children?: Snippet;
		meta?: string[];
		actions?: Snippet;
		truncate?: boolean;
		centered?: boolean;
		onclick?: () => void;
	}

	const { name, children, meta = [], actions, centered = false, truncate = false, onclick }: Props = $props();
</script>

{#snippet content()}
	<div class="row-copy">
		{#if children}{@render children()}{:else if name}<strong class="row-name">{name}</strong>{/if}
		{#if meta.length}<div class="row-meta">{#each meta as part}<span>{part}</span>{/each}</div>{/if}
	</div>
	{#if actions}<div class="row-actions">{@render actions()}</div>{/if}
{/snippet}

{#if onclick}
	<button type="button" class="row-main pick" class:centered class:truncate {onclick}>{@render content()}</button>
{:else}
	<div class="row-main" class:centered class:truncate>{@render content()}</div>
{/if}

<style>
	.row-main { display: flex; align-items: start; justify-content: space-between; gap: var(--space-3); }
	.row-main.centered { align-items: center; }
	.row-main.pick { width: 100%; padding: 0; text-align: left; cursor: pointer; font: inherit; color: inherit; background: transparent; border: 0; }
	.row-copy { min-width: 0; }
	.truncate .row-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.row-name { display: block; font-size: var(--text-subhead); color: var(--text-primary); overflow-wrap: anywhere; }
	.row-actions { flex-shrink: 0; display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
	.row-meta { display: flex; flex-wrap: wrap; column-gap: 0; row-gap: var(--space-1); margin-top: var(--space-1); color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.row-meta span:not(:last-child)::after { content: '·'; margin: 0 var(--space-2); color: var(--text-tertiary); }
</style>
