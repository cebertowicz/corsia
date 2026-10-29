<script lang="ts">
	import { Button } from '$lib/components/ui';
	import SetupAiRows from './SetupAiRows.svelte';
	const { error, retrying, retry }: { error: string; retrying: boolean; retry: () => void } = $props();
</script>

{#if error}
	{#if retrying}<SetupAiRows rows={[{ key: 'source-error', name: error, meta: '', busy: true }]} />
	{:else}<p class="error-text" role="alert">{error}</p>{/if}
	<Button variant="chip" disabled={retrying} onclick={retry}>Retry</Button>
{/if}

<style>
	.error-text { margin: var(--space-2) var(--space-4); font-size: var(--text-subhead); line-height: var(--lh-normal); }
</style>
