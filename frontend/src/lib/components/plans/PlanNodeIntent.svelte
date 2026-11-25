<script lang="ts">
	import type { OperatorNode } from '$lib/api/plans';
	import { isFinished } from './nodeDetailsText';

	let { node }: { node: OperatorNode } = $props();
</script>

<div class="intent">
	{#if node.intent || (!isFinished(node) && !node.intent_parent_title)}
		<div class="head">
			<span class="prefix">{node.intent_parent_title ? `From ${node.intent_owner_name} · ${node.intent_parent_title}` : node.intent_is_person_comment ? 'Reading (your comment, as given):' : 'Reading:'}</span>
		</div>
		{#if node.intent}
			<p class="reading">{node.intent}</p>
		{:else}
			<p class="reading muted">No reading recorded.</p>
		{/if}
		{#if node.intent_parent_title}<p class="note">To change it, tell {node.intent_owner_name}.</p>{/if}
	{/if}
	{#if node.acceptance_criteria}
		<p class="done"><span class="prefix">Done when:</span> {node.acceptance_criteria}</p>
	{/if}
</div>

<style>
	.intent { display: flex; flex-direction: column; gap: var(--space-1); }
	p { margin: 0; }
	.prefix, .note { color: var(--text-tertiary); }
	.head { display: flex; align-items: baseline; gap: var(--space-2); }
	/* The reading is the content; labels and notes step back. */
	.reading { color: var(--text-primary); font-size: var(--text-footnote); line-height: var(--lh-normal); overflow-wrap: anywhere; }
	.reading.muted { color: var(--text-tertiary); font-style: italic; }
	.done { margin-top: var(--space-2); }
</style>
