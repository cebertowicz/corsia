<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';

	let { loading = false, failed = false, hasMore = false, hasRows = false, onLoadOlder }: {
		loading?: boolean;
		failed?: boolean;
		hasMore?: boolean;
		hasRows?: boolean;
		onLoadOlder?: () => void;
	} = $props();
</script>

{#if loading}
	<div class="loading-older">
		<svg class="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		<span>Loading older messages...</span>
	</div>
{:else if failed && hasMore}
	<div class="loading-older older-error">
		<span>Couldn't load older messages.</span>
		<Button size="sm" variant="secondary" ariaLabel="Retry loading older messages" onclick={() => onLoadOlder?.()}>Retry</Button>
	</div>
{:else if hasMore && hasRows}
	<!-- Scrolling pages on its own, but only when the view can move; this is the way out when it cannot. -->
	<div class="loading-older">
		<Button size="sm" variant="secondary" ariaLabel="Load older messages" onclick={() => onLoadOlder?.()}>Load older messages</Button>
	</div>
{/if}

<style>
	.loading-older {
		display: flex; align-items: center; justify-content: center; gap: 8px;
		padding: 12px; color: var(--text-muted); font-size: var(--text-footnote);
	}
	.older-error { color: var(--red); }
</style>
