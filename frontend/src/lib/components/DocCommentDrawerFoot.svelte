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
	<div class="delivery" title="When off, comments send when you close the document or pause for two minutes."><span>Deliver immediately</span><Toggle size="sm" touch checked={$deliverCommentsImmediately} onchange={changeImmediateDelivery} ariaLabel="Deliver comments immediately" /></div>
</div>

<style>
	.drawer-foot { flex: none; display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-2); border-top: 1px solid var(--border-subtle); font-size: var(--text-footnote); }
	.send { container-type: inline-size; }
	.send:not(:has(*)) { display: none; }
	.send :global(button) { min-height: var(--touch-target); padding-inline: 0; border: 0; background: none; box-shadow: none; color: var(--text-primary); font: inherit; }
	.delivery { display: flex; align-items: center; justify-content: space-between; min-height: var(--touch-target); gap: var(--space-2); --toggle-hit: var(--touch-target); }
</style>
