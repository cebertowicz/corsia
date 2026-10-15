<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * A setup-rail section: a small-caps label over `RailRow`s with a hairline between sections.
	 * Any use outside setup needs a Product Experience decision.
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
		text-transform: uppercase; color: var(--text-tertiary);
	}
	.rows { display: flex; flex-direction: column; gap: 1px; padding: 2px 8px 8px; }
</style>
