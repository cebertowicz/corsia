<script lang="ts">
	import type { Snippet } from 'svelte';
	/** The same resolved thread list in the comments drawer at every width. */
	let { list }: { list: Snippet } = $props();
</script>

<div class="resolved-rows" data-resolved-column aria-label="Resolved comments">{@render list()}</div>

<style>
	.resolved-rows { min-width: 0; --ct-radius: var(--radius); }
</style>
