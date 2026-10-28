<script lang="ts">
	import type { DocComment } from '$lib/stores/docComments';
	import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';
	import DocReplyChip from './DocReplyChip.svelte';
	import DeliveryConfirmation from './attention/DeliveryConfirmation.svelte';

	let { artifactId, path, threadId, threads, ready, covered, sheetModalOpen, onStep }: {
		artifactId: string | null;
		path: string;
		threadId: string | null;
		threads: DocComment[];
		ready: boolean;
		covered: boolean;
		sheetModalOpen: boolean;
		onStep: () => void;
	} = $props();
	const hasChip = $derived(ready && !covered && !sheetModalOpen && !!artifactId && threads.length > 0);
	const hasToast = $derived(!!artifactId && $deliveryConfirmation?.artifactId === artifactId);
</script>

{#if hasChip || hasToast}
	<div class="doc-bottom-band" data-doc-bottom-band>
		{#if hasToast}<DeliveryConfirmation {artifactId} inBand />{/if}
		{#if hasChip && artifactId}<DocReplyChip {artifactId} {path} {threadId} {threads} {onStep} />{/if}
	</div>
{/if}

<style>
	.doc-bottom-band { grid-area: bottom; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-2); padding: 0 var(--space-3); }
	.doc-bottom-band :global(.delivery-confirmation) { align-self: center; max-width: 100%; }
</style>
