<script lang="ts">
	// A document's loading and failed-to-open states: the viewer's own, and a pressed link's tab before it names a file.
	import Button from '$lib/components/ui/Button.svelte';
	import './DocViewer.css';

	let { state, name, onRetry }: { state: 'loading' | 'error'; name: string; onRetry: () => void } = $props();
</script>

{#if state === 'loading'}
	<div class="loadbar"><span></span></div>
	<div class="md-skel">{#each Array(6) as _, i (i)}<span class="ms-line" style="width:{[90, 70, 80, 55, 75, 45][i]}%"></span>{/each}</div>
{:else}
	<div class="load-error" role="alert">
		<p>Couldn't open {name}.</p>
		<Button size="sm" variant="ghost" onclick={onRetry}>Retry</Button>
	</div>
{/if}
