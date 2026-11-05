<script lang="ts">
	import ToastNotice from '$lib/components/ToastNotice.svelte';
	import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';

	let { artifactId, inBand = false }: { artifactId: string | null; inBand?: boolean } = $props();
</script>

{#if $deliveryConfirmation?.artifactId === artifactId}
	{@const confirmation = $deliveryConfirmation}
	<div class="delivery-confirmation" class:in-band={inBand}>
		<!-- Same component as the global ToastContainer; the artifact canvas alone owns this bottom-of-canvas placement. -->
		<ToastNotice toast={{ id: confirmation?.id ?? '', message: confirmation?.message ?? '', type: 'success' }} onDismiss={() => { if (confirmation) deliveryConfirmation.dismiss(confirmation.id); }} animateOut={false} />
	</div>
{/if}

<style>
	/* The document canvas is the positioned containing block. This transient result may cover content, never app controls. */
	.delivery-confirmation {
		position: absolute;
		z-index: 3;
		inset-inline: var(--space-3);
		bottom: var(--space-3);
		display: flex;
		justify-content: center;
		min-width: 0;
		pointer-events: none;
	}
	.delivery-confirmation.in-band { position: static; min-width: 0; }
	.delivery-confirmation :global(.toast) { pointer-events: auto; }
</style>
