<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * A section of the shell's sidebar: the small caps label over a stack of `RailRow`s, with the hairline
	 * the sidebar draws between its sections. Proposed (pending Product Experience and Steve): the shell
	 * sidebar's own markup, drawn from Sidebar.svelte's `.rail-scope` and `RailScopedSection.svelte`'s
	 * `.scoped`, lifted into a component so setup can draw the same sidebar. Only setup uses it.
	 */
	interface Props {
		label: string;
		/** The first section has no hairline above it. */
		first?: boolean;
		children: Snippet;
	}

	const { label, first = false, children }: Props = $props();
</script>

<section class="rail-section" class:first aria-label={label}>
	<h2 class="cap">{label}</h2>
	<div class="rows">{@render children()}</div>
</section>

<style>
	.rail-section { border-top: 1px solid var(--border-subtle); }
	.rail-section.first { border-top: 0; }
	.cap {
		margin: 0; padding: 12px 14px 4px;
		font-size: var(--text-caption); font-weight: var(--fw-bold); letter-spacing: var(--ls-cap);
		text-transform: uppercase; color: color-mix(in srgb, var(--violet) 55%, var(--text-tertiary));
	}
	.rows { display: flex; flex-direction: column; gap: 1px; padding: 2px 8px 8px; }
</style>
