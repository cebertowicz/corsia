<script lang="ts">
	import QuoteLine from '$lib/components/ui/QuoteLine.svelte';
	let { quote, label = '', bubble = false, lines = 1 }: { quote: string; label?: string; bubble?: boolean; lines?: number | null } = $props();
</script>

<div class="quote" class:label class:bubble><QuoteLine {lines}>{label || `“${quote}”`}</QuoteLine></div>

<style>
	.quote { color: var(--text-secondary); font-size: var(--text-caption); font-style: italic; }
	.quote.label { font-style: normal; }
	.quote.bubble { color: var(--accent-on-fill); font-size: var(--text-callout); line-height: 1.5; }
	.quote.bubble :global(.quote-line) { border-left-color: var(--accent-on-fill); }
</style>
