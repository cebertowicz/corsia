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
	// Over a live page, or the stale frame an ended stream leaves behind.
	const overPage = $derived(streaming || streamEnded);
	const retryable = $derived(unavailable || status.startsWith('Browser could not open'));
</script>

{#if status}
	<div class="page-status" class:over-page={overPage} role="status">
		<div class="surface" class:pill={overPage}>
			{#if opening}
				<Spinner size={20} /><span class="sr-only">Opening</span>
			{:else if noBrowser}
				<p>Type an address above, or ask the agent to open a page.</p>
			{:else}
				<p>{status}</p>
			{/if}
			{#if streamEnded}<Button onclick={onreconnect}>Reconnect</Button>{/if}
			{#if retryable}<Button onclick={unavailable ? onreconnect : onretry}>Try again</Button>{/if}
		</div>
	</div>
{/if}

<style>
	.page-status {
		position: absolute; inset: 0; z-index: 1; display: flex; align-items: center; justify-content: center;
		padding: var(--space-7) var(--space-4); pointer-events: none;
	}
	.surface { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: var(--space-3); max-width: 100%; text-align: center; }
	.surface :global(.btn) { pointer-events: auto; }
	.surface p { margin: 0; max-width: 44ch; color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; overflow-wrap: anywhere; }
	/* Over a live or stale page the whole status sits on one glass surface at the bottom edge, never a cover. */
	.page-status.over-page { align-items: flex-end; padding: var(--space-4); }
	.surface.pill { padding: var(--space-2) var(--space-4); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--glass-regular-thick); box-shadow: var(--glass-elevation-1); backdrop-filter: var(--glass-blur); }
	.surface.pill p { color: var(--text-primary); }
	@media (max-width: 768px) { .surface :global(.btn) { min-height: 44px; } }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
