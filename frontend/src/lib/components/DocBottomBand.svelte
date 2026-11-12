<script lang="ts">
	import type { DocComment } from '$lib/stores/docComments';
	import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';
	import { createJumpRead } from '$lib/docJumpRead.svelte';
	import { jumpedThread } from '$lib/docJumpTargets';
	import DocJumpControl from './DocJumpControl.svelte';
	import DeliveryConfirmation from './attention/DeliveryConfirmation.svelte';

	// The canvas's bottom slot: the one jump control and, over an image, the notice that a comment you sent was delivered. A document
	// says that in its header instead (the Sent mark where Send stood), since a notice here would float over its text and margin cards.
	let { artifactId, threads, root, ready, covered, sheetModalOpen, onJump, confirmsDelivery = false, ignoreConfirmationId = null }: {
		artifactId: string | null;
		threads: DocComment[];
		/** The canvas the unread items are drawn in: the document, or the image shown over it. */
		root: HTMLElement | undefined;
		ready: boolean;
		covered: boolean;
		sheetModalOpen: boolean;
		onJump: (thread: DocComment) => void;
		/** Shows the delivery notice: an image viewer's band, which has no header to say it in. */
		confirmsDelivery?: boolean;
		ignoreConfirmationId?: string | null;
	} = $props();

	// The band owns the read because it owns the row: an empty row must never stand between the text and a drawer.
	const mayJump = $derived(ready && !covered && !sheetModalOpen && !!artifactId && threads.length > 0);
	const jump = createJumpRead(() => (mayJump ? root : undefined), () => threads);
	const jumped = $derived(jumpedThread(threads, jump().target));
	const hasJump = $derived(mayJump && !!jumped);
	const hasToast = $derived(confirmsDelivery && !!artifactId && $deliveryConfirmation?.artifactId === artifactId && $deliveryConfirmation.id !== ignoreConfirmationId);
	// The name carries the same count the button shows, so the two can never disagree (COPY-STANDARD C5).
	const label = $derived(`${jump().count} new, next ${jump().direction}: ${jump().target?.label ?? ''}`);
</script>

{#if hasJump || hasToast}
	<div class="doc-bottom-band" data-doc-bottom-band>
		{#if hasToast}<DeliveryConfirmation {artifactId} inBand />{/if}
		{#if hasJump && jumped}
			<DocJumpControl count={jump().count} direction={jump().direction ?? 'below'} {label} onJump={() => onJump(jumped)} />
		{/if}
	</div>
{/if}

<style>
	/* It floats over its canvas (placed by DocViewer.css on a document, by the viewer over an image); only its notices take the pointer. */
	.doc-bottom-band { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-2); padding: 0 var(--space-3); }
	.doc-bottom-band > :global(*) { pointer-events: auto; }
	.doc-bottom-band :global(.delivery-confirmation) { align-self: center; max-width: 100%; }
</style>
