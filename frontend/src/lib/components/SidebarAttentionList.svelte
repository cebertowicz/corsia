<script lang="ts">
	// What a row's slot-3 mark stands for. Mounted once beside the sidebar, never
	// inside a row, so its clicks and keys cannot reach the row's own controls.
	import Modal from './ui/Modal.svelte';
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import { attentionKindWord } from '$lib/components/plans/planDisplay';
	import { emitProductEvent } from '$lib/product_events';
	import { focusPlanNode } from '$lib/planNodeFocus';

	let {
		attention = $bindable(),
		onSelect,
	}: {
		attention: { threadId: string; title: string } | null;
		onSelect: (threadId: string) => void | Promise<void>;
	} = $props();

	const items = $derived(attention ? ($threadPlanStates[attention.threadId]?.attention_items ?? []) : []);

	async function select(threadId: string, item: { kind: string; node_id: string | null }): Promise<void> {
		emitProductEvent('sidebar', 'attention_item_selected', { thread_id: threadId, kind: item.kind, node_id: item.node_id });
		attention = null;
		await onSelect(threadId);
		await focusPlanNode(threadId, item.node_id);
	}
</script>

{#if attention}
	{@const { threadId, title } = attention}
	<Modal {title} size="sm" onClose={() => (attention = null)}>
		<div class="attention-list" aria-label={`${title} items`}>
			<!-- Access requests carry no node, so two with one purpose key by position. -->
			{#each items as item, i (`${item.kind}:${item.node_id ?? i}:${item.title}`)}
				<button class="attention-item" onclick={() => select(threadId, item)}>
					<span>{item.title}</span>
					<small>{attentionKindWord(item.kind)}</small>
				</button>
			{/each}
		</div>
	</Modal>
{/if}

<style>
	.attention-list { display: flex; flex-direction: column; gap: 2px; }
	.attention-item {
		display: flex; justify-content: space-between; gap: 12px; width: 100%; padding: 10px;
		border: none; border-radius: var(--radius-2xs); background: none; color: var(--text-primary);
		font: inherit; text-align: left; cursor: pointer;
	}
	.attention-item:hover, .attention-item:focus-visible { background: var(--bg-hover); outline: none; }
	.attention-item small { color: var(--text-tertiary); }
</style>
