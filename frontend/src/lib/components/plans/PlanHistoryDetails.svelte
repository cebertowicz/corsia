<!-- A history row's details: the node is read when the row opens, with Loading and a Retry on failure. -->
<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import type { OperatorNode } from '$lib/api/plans';
	import Button from '$lib/components/ui/Button.svelte';
	import { createHistoryNode } from './historyNode.svelte';
	import PlanNodeDetails from './PlanNodeDetails.svelte';

	let { node, planId, id }: {
		/** The history row as the tree draws it. */
		node: OperatorNode;
		planId: string;
		id: string;
	} = $props();

	const loaded = createHistoryNode({ planId: () => planId, nodeId: () => node.id });
	onMount(() => { void loaded.load(); });
	onDestroy(() => loaded.cancel());
	const state = $derived(loaded.state);
</script>

{#if state.status === 'ready'}
	<PlanNodeDetails node={state.node} {id} depth={node.depth} />
{:else if state.status === 'error'}
	<section {id} class="history-details" style={`--depth:${node.depth}`} role="alert" aria-label={node.title + ' details'}>
		<span>Couldn’t load these details.</span>
		<Button variant="ghost" size="sm" onclick={() => void loaded.load()}>Retry</Button>
	</section>
{:else}
	<section {id} class="history-details" style={`--depth:${node.depth}`} role="status" aria-label={node.title + ' details'}>Loading…</section>
{/if}

<style>
	.history-details {
		display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-2);
		margin: 0 var(--space-2) var(--space-2) calc(34px + var(--depth) * 22px);
		padding: var(--space-1) var(--space-2); border-left: 2px solid var(--border);
		color: var(--text-secondary); font-size: var(--text-caption);
	}
	.history-details[role='alert'] { color: var(--red-text); }
	@media (max-width: 620px) {
		.history-details { margin-left: calc(18px + var(--depth) * 12px); }
	}
</style>
