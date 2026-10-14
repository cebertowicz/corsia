<!-- A row's details. A row the history route fetched carries none, so opening it reads the node. -->
<script lang="ts">
	import type { OperatorNode } from '$lib/api/plans';
	import PlanHistoryDetails from './PlanHistoryDetails.svelte';
	import PlanNodeDetails from './PlanNodeDetails.svelte';

	let { node, id, planId, fromHistory, indent }: {
		node: OperatorNode;
		id: string;
		planId: string;
		/** The row came from the history route (`foldHistory`); a listed row never reads. */
		fromHistory: boolean;
		/** The row's indent as a CSS length: the details sit directly under it (`planIndent`). */
		indent: string;
	} = $props();
</script>

{#if fromHistory}
	<PlanHistoryDetails {node} {planId} {id} {indent} />
{:else}
	<PlanNodeDetails {node} {id} {indent} />
{/if}
