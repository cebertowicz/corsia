<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	let { status, streaming, onreconnect, onretry }: {
		/** What the pane has to say: no browser, opening, an error, or an ended stream; empty when the page is simply there. */
		status: string;
		/** A live page is on the canvas, so the status sits over its edge instead of replacing it. */
		streaming: boolean;
		onreconnect: () => void;
		onretry: () => void;
	} = $props();

	const opening = $derived(status === 'Opening');
	const noBrowser = $derived(status === 'No browser yet');
	const streamEnded = $derived(status === 'Browser stream ended');
	const unavailable = $derived(status.startsWith('Browser stream unavailable'));
	const retryable = $derived(unavailable || status.startsWith('Browser could not open'));
</script>

{#if status}
	<div class="page-status" class:over-page={streaming} role="status">
		{#if opening}
			<Spinner size={20} /><span class="sr-only">Opening</span>
		{:else if noBrowser}
			<p>Type an address above, or ask the agent to open a page.</p>
		{:else}
			<p>{status}</p>
			{#if streamEnded}<Button size="sm" onclick={onreconnect}>Reconnect</Button>{/if}
			{#if retryable}<Button size="sm" onclick={unavailable ? onreconnect : onretry}>Try again</Button>{/if}
		{/if}
	</div>
{/if}

<style>
	.page-status {
		position: absolute; inset: 0; z-index: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
		gap: 12px; padding: 32px 16px; text-align: center; pointer-events: none;
	}
	.page-status :global(.btn) { pointer-events: auto; }
	.page-status p { margin: 0; max-width: 32ch; color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; overflow-wrap: anywhere; }
	/* Over a live page the status is a pill at the bottom edge, never a cover. */
	.page-status.over-page { inset: auto 16px 16px; padding: 0; }
	.page-status.over-page p { max-width: none; padding: 8px 16px; border: 1px solid var(--border-subtle); border-radius: var(--radius-pill); background: var(--glass-regular-thick); box-shadow: var(--glass-elevation-1); backdrop-filter: var(--glass-blur); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
