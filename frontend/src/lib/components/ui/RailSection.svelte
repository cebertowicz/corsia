<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * A section of the shell's sidebar: the small caps label over a stack of `RailRow`s, with the hairline
	 * the sidebar draws between its sections. Approved for setup and for extracting existing sidebar structure
	 * without a visual change; any new shared use needs a Product Experience decision.
	 */
	interface Props {
		label: string;
		/** Existing sidebar layouts reuse their exact markup without a visual change. */
		layout?: 'setup' | 'sidebar-nav' | 'sidebar-scoped';
		/** The first section has no hairline above it. */
		first?: boolean;
		children: Snippet;
	}

	const { label, layout = 'setup', first = false, children }: Props = $props();
</script>

{#if layout === 'sidebar-nav'}
	<nav class="rail-nav" aria-label={label}>{@render children()}</nav>
{:else if layout === 'sidebar-scoped'}
	<nav class="scoped" aria-label={label}>{@render children()}</nav>
{:else}
	<section class="rail-section" class:first aria-label={label}>
		<h2 class="cap">{label}</h2>
		<div class="rows">{@render children()}</div>
	</section>
{/if}

<style>
	.rail-section { border-top: 1px solid var(--border-subtle); }
	.rail-section.first { border-top: 0; }
	.cap {
		margin: 0; padding: 12px 14px 4px;
		font-size: var(--text-caption); font-weight: var(--fw-bold); letter-spacing: var(--ls-cap);
		text-transform: uppercase; color: var(--text-tertiary);
	}
	.rows { display: flex; flex-direction: column; gap: 1px; padding: 2px 8px 8px; }
</style>
