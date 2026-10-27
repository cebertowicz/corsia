<script lang="ts">
	import type { Snippet } from 'svelte';
	let { lines = 1, children }: { lines?: number | null; children: Snippet } = $props();
</script>

<div class="quote-line" style={`--quote-lines: ${lines ?? 'unset'}`}>{@render children()}</div>

<style>
	.quote-line { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: var(--quote-lines); line-clamp: var(--quote-lines); overflow: hidden; border-left: 2px solid color-mix(in srgb, var(--amber) 55%, transparent); padding-left: var(--space-2); }
</style>
