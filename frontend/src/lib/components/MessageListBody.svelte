<script lang="ts">
	import TranscriptContent from './TranscriptContent.svelte';
	import CheckpointStatus from './CheckpointStatus.svelte';
	import PendingWakeRetry from './PendingWakeRetry.svelte';
	import type { PlanLimitPauseDescriptor } from './planLimitPause';
	import type { RunLoad, RunStub } from '$lib/focusRuns';
	import type { WorkBlockLive } from './workBlockLive';

	let {
		transcriptEntries,
		pendingEntries,
		retryRows = [],
		expandedTools,
		expandedReasoning,
		onToggleToolBlock,
		onToggleReasoning,
		onLoadToolResult,
		onOpenLightbox,
		threadId,
		renderedStreaming,
		liveRollup = undefined,
		checkpoint,
		turnSummaries = {},
		pauses = [],
		historyComplete = false,
		runLoads = {},
		onLoadRun,
	}: {
		transcriptEntries: any[];
		pendingEntries: any[];
		/** Focus: context updates that could not be delivered, each with its Retry. */
		retryRows?: any[];
		expandedTools: Set<string>;
		expandedReasoning: Set<string>;
		onToggleToolBlock: (id: string) => void;
		onToggleReasoning: (id: string) => void;
		onLoadToolResult?: (toolCallId: string) => void;
		onOpenLightbox: (url: string) => void;
		threadId?: string | null;
		renderedStreaming: boolean;
		liveRollup?: WorkBlockLive;
		checkpoint: any;
		turnSummaries?: Record<string, any>;
		pauses?: readonly PlanLimitPauseDescriptor[];
		historyComplete?: boolean;
		runLoads?: Record<string, RunLoad>;
		onLoadRun?: (stub: RunStub) => void;
	} = $props();
</script>

<TranscriptContent
	entries={transcriptEntries}
	{expandedTools}
	{expandedReasoning}
	{onToggleToolBlock}
	{onToggleReasoning}
	{onLoadToolResult}
	{onOpenLightbox}
	{threadId}
	{renderedStreaming}
	{liveRollup}
	{turnSummaries}
	{pauses}
	{historyComplete}
	{runLoads}
	{onLoadRun}
/>
{#if checkpoint && threadId}
	<CheckpointStatus {checkpoint} threadId={threadId} placement="transcript" />
{/if}
<!-- TranscriptContent owns the live line in both Focus states. -->
{#if pendingEntries.length > 0 || retryRows.length > 0}
	<section class="pending-input-region" aria-label="Messages not delivered yet">
		<TranscriptContent
			entries={pendingEntries}
			{expandedTools}
			{expandedReasoning}
			{onToggleToolBlock}
			{onToggleReasoning}
			{onLoadToolResult}
			{onOpenLightbox}
			{threadId}
			renderedStreaming={false}
			showWait={false}
			{turnSummaries}
			historyComplete
		/>
		{#each retryRows as row (row.id)}<PendingWakeRetry msg={row} {threadId} />{/each}
	</section>
{/if}

<style>
	.pending-input-region {
		margin: 4px auto 0;
		padding-top: 12px;
		max-width: 760px;
		border-top: 1px solid var(--border-subtle);
	}
	.pending-input-region :global(.message) { margin-bottom: 12px; }
</style>
