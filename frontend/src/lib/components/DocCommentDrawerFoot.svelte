<script lang="ts">
	import { Toggle } from '$lib/components/ui';
	import { deliverCommentsImmediately, setDeliverCommentsImmediately } from '$lib/commentDeliveryPreference';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import DocFeedbackControls from './DocFeedbackControls.svelte';

	let { feedback }: { feedback?: DocFeedback } = $props();

	async function changeImmediateDelivery(value: boolean): Promise<void> {
		if (!(await setDeliverCommentsImmediately(value))) return;
		// A remote client may have held a comment that has not synced here yet.
		if (value) void feedback?.sendDocument();
	}
</script>

<div class="drawer-foot">
	{#if feedback}<div class="send"><DocFeedbackControls {feedback} lane="document-held" countSep=" · " announce={false} /></div>{/if}
	<div class="delivery" title="When off, comments send when you close the document or pause for two minutes."><Toggle size="sm" label="Deliver immediately" checked={$deliverCommentsImmediately} onchange={changeImmediateDelivery} ariaLabel="Deliver comments immediately" /></div>
</div>

<style>
	.drawer-foot { flex: none; display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1); padding: var(--space-2); border-top: 1px solid var(--border-subtle); font-size: var(--text-footnote); }
	.send { min-width: 0; order: 1; margin-left: auto; }
	.send:not(:has(*)) { display: none; }
	.send :global(button) { min-height: var(--touch-target); font-weight: 500; }
	.send :global(button.btn.sm)::after { inset: 0; }
	.send :global(.send-full) { display: none; }
	.send :global(.send-short) { display: inline; }
	.delivery { flex: 1 1 12rem; min-width: 12rem; display: flex; align-items: center; justify-content: space-between; min-height: var(--touch-target); gap: var(--space-2); --toggle-hit: var(--touch-target); }
</style>
