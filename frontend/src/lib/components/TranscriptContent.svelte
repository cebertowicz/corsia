<script lang="ts">
	import UserMessageBubble from './UserMessageBubble.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import SystemMessageRow from './SystemMessageRow.svelte';
	import LiveToolActivity from './LiveToolActivity.svelte';
	import UpdateCard from './UpdateCard.svelte';
	import PassiveOutcomeList from './PassiveOutcomeList.svelte';
	import VisitDivider from './VisitDivider.svelte';
	import WorkBlock from './WorkBlock.svelte';
	import { activeToolsByThread } from '$lib/stores/threads';
	import { INITIAL_TURN_STATE, turnStates } from '$lib/ws/turnState';
	import { threadLiveStep, UNKNOWN_STEP } from './plans/planLiveStep';
	import { createLiveLine } from './plans/liveLine.svelte';
	import type { TranscriptEntry } from './messageListModel';
	import { isRunExpanded, runTokens, toggleRun } from './transcriptFocusModel';
	import { liveRunTimes, runLabels, type TurnSummaries } from './transcriptRunLabel';
	import { minuteClock } from '$lib/time';
	import { attachPlanLimitPauses, stubPauseTurns, type PlanLimitPauseDescriptor } from './planLimitPause';
	import { deliveryPresentation } from '$lib/messageDelivery';
	import type { TranscriptMessage } from './messageListModel';
	import type { PauseAnnotation } from './transcriptFocusModel';
	import EventChip from './EventChip.svelte';
	import { sourceMembers } from './contextEventModel';
	import { PAUSED_AT_PLAN_LIMIT } from '$lib/stores/overageSetting';
	import type { RunLoad, RunStub } from '$lib/focusRuns';

	let {
		entries,
		expandedTools,
		expandedReasoning,
		onToggleToolBlock,
		onToggleReasoning,
		onLoadToolResult,
		onOpenLightbox,
		threadId,
		renderedStreaming,
		turnSummaries = {},
		pauses = [],
		historyComplete = false,
		runLoads = {},
		onLoadRun,
	}: {
		entries: TranscriptEntry[];
		expandedTools: Set<string>;
		expandedReasoning: Set<string>;
		onToggleToolBlock: (id: string) => void;
		onToggleReasoning: (id: string) => void;
		onLoadToolResult?: (messageId: string, toolCallId: string) => void;
		onOpenLightbox: (url: string) => void;
		threadId: string | null | undefined;
		renderedStreaming: boolean;
		turnSummaries?: TurnSummaries;
		pauses?: readonly PlanLimitPauseDescriptor[];
		/** True when no older rows exist above `entries`, so the first run's start is known. */
		historyComplete?: boolean;
		/** Where each stub run's range read stands, by the run's newest row id. */
		runLoads?: Record<string, RunLoad>;
		/** Read (more of) a stub run's rows. */
		onLoadRun?: (stub: RunStub) => void;
	} = $props();

	const uid = $props.id();
	const boundThreadId = $derived(threadId ?? null);
	const boundTurn = $derived(boundThreadId ? $turnStates.get(boundThreadId) ?? INITIAL_TURN_STATE : null);
	const pausePresentation = $derived(attachPlanLimitPauses(entries, pauses, boundThreadId && boundTurn
		? { threadId: boundThreadId, phase: boundTurn.phase, turnId: boundTurn.turnId } : undefined));
	const renderedEntries = $derived(pausePresentation.entries);
	const stubPauses = $derived(stubPauseTurns(renderedEntries, pauses, turnSummaries));
	// A hold's ask card has one home: its blocked update row while that row is on screen, else its message.
	const homedHolds = $derived(new Set(renderedEntries.flatMap((entry) =>
		entry.kind === 'update' && entry.card.kind === 'blocked' && entry.card.blocked_asks_user && entry.card.ask?.hold_id
			? [entry.card.ask.hold_id] : [])));
	const visibleSources = $derived.by(() => {
		const ids = new Set<string>();
		renderedEntries.forEach((entry, index) => {
			const items = entry.kind === 'message' ? [entry] : entry.kind === 'fold'
				&& isRunExpanded(runTokens(renderedEntries, index, historyComplete), expandedFolds) ? entry.items : [];
			for (const item of items) for (const row of sourceMembers(item.message)) ids.add(row.id);
		});
		return ids;
	});
	// Open runs (their `row:` and `after:` tokens); written only by clicks.
	let expandedFolds = $state(new Set<string>());
	// One instance serves every thread, and `after:start` names no thread: a thread change resets.
	let expandedForThread: string | null | undefined;
	$effect.pre(() => {
		const previous = expandedForThread;
		expandedForThread = boundThreadId;
		if (previous !== undefined && previous !== boundThreadId) expandedFolds = new Set();
	});
	// AS14 owns what a running Turn is doing now; Focus reads the same step as its plan row.
	const liveNow = $derived(
		boundThreadId ? threadLiveStep(boundThreadId, $turnStates, $activeToolsByThread) : null,
	);
	const liveRunIndex = $derived(renderedEntries.findIndex((entry) => entry.kind === 'fold' && entry.live));
	const hasLiveFold = $derived(liveRunIndex !== -1);
	// The live line restarts after each shown item: its segment is the item before the open run.
	const liveSegment = $derived(hasLiveFold
		? runTokens(renderedEntries, liveRunIndex, historyComplete).find((token) => token.startsWith('after:')) ?? 'after:'
		: undefined);
	const liveLine = createLiveLine(() => liveNow, () => !renderedStreaming, () => liveSegment);
	const labels = $derived(runLabels(renderedEntries, turnSummaries, historyComplete));
	const liveTimes = $derived(hasLiveFold ? liveRunTimes(renderedEntries, turnSummaries, historyComplete, $minuteClock.getTime()) : new Map<string, string | null>());
	// An open run whose rows were never read starts reading them, however it was opened.
	$effect(() => {
		renderedEntries.forEach((entry, index) => {
			if (entry.kind !== 'fold' || !entry.stub || runLoads[entry.stub.lastId]) return;
			if (isRunExpanded(runTokens(renderedEntries, index, historyComplete), expandedFolds)) onLoadRun?.(entry.stub);
		});
	});
</script>

{#snippet messageRow(entry: Extract<TranscriptEntry, { kind: 'message' }>)}
	{@const msg = entry.message}
	{@const isAgentInbound = msg.role === 'user' && msg.metadata?.sender_kind === 'agent'}
	<div class="message {msg.role}" data-message-id={msg.id} class:agent-inbound={isAgentInbound} class:human-user={msg.role === 'user' && !isAgentInbound}>
		{#if msg.role === 'system'}
			<SystemMessageRow {msg} />
		{:else if msg.role === 'user'}
			<UserMessageBubble {msg} {onOpenLightbox} />
		{:else}
			<AssistantMessage
				{msg}
				{expandedTools}
				{expandedReasoning}
				showActivity={entry.showActivity}
				showContent={entry.showContent}
				hiddenHolds={homedHolds}
				onToggleToolBlock={onToggleToolBlock}
				onToggleReasoning={onToggleReasoning}
				onLoadToolResult={onLoadToolResult ? (toolCallId) => onLoadToolResult(msg.id, toolCallId) : undefined}
			/>
		{/if}
	</div>
{/snippet}

{#snippet deliveryWarning(msg: TranscriptMessage)}
	{#if deliveryPresentation(msg, renderedStreaming)}
		{#if msg.role === 'system'}<EventChip {msg} deliveryOnly />
		{:else if msg.role === 'user'}<UserMessageBubble {msg} {onOpenLightbox} deliveryOnly />{/if}
	{/if}
{/snippet}

{#snippet historicalPause(annotation: PauseAnnotation, proxy: boolean)}
	{#if proxy}
		{@render deliveryWarning(annotation.pause.source)}
		<div class="historical-anchor">{annotation.pause.anchorKind === 'input' ? 'Earlier input' : 'Earlier work'}</div>
	{/if}
	<div class="plan-limit-paused">{PAUSED_AT_PLAN_LIMIT}</div>
{/snippet}

{#each renderedEntries as entry, index (entry.id)}
	{#if entry.kind === 'message'}
		{@render messageRow(entry)}
	{:else if entry.kind === 'update'}
		<UpdateCard card={entry.card} earlier={entry.earlier} />
	{:else if entry.kind === 'passive'}
		{#key boundThreadId}<PassiveOutcomeList cards={entry.cards} id={`${uid}-${entry.id}`} />{/key}
	{:else if entry.kind === 'divider'}
		<VisitDivider truncated={entry.truncated} />
	{:else if entry.kind === 'pause'}
		{@render historicalPause(entry, entry.showProxy || !visibleSources.has(entry.pause.source.id))}
	{:else if entry.kind === 'stub-pause'}
		<div class="plan-limit-paused">{PAUSED_AT_PLAN_LIMIT}</div>
	{:else if entry.kind === 'fold'}
		{@const tokens = runTokens(renderedEntries, index, historyComplete)}
		{@const expanded = isRunExpanded(tokens, expandedFolds)}
		{@const label = labels.get(entry.id) ?? 'Worked'}
		{@const bodyId = `${uid}-${entry.id}`}
		<WorkBlock
			{entry}
			{label}
			{expanded}
			{bodyId}
			liveTime={liveTimes.get(entry.id) ?? null}
			livePhrase={liveLine.shown?.phrase ?? UNKNOWN_STEP}
			liveDimmed={liveLine.shown?.between === true}
			load={entry.stub ? runLoads[entry.stub.lastId] : undefined}
			onToggle={() => { expandedFolds = toggleRun(tokens, expandedFolds); }}
			{onLoadRun}
			row={messageRow}
			pause={historicalPause}
			warning={deliveryWarning}
		>
			{#snippet liveActivity()}
				<LiveToolActivity {threadId} {expandedTools} onToggleToolBlock={onToggleToolBlock} />
			{/snippet}
			{#snippet notes()}
				{#each stubPauses.get(entry.id) ?? [] as turnId (turnId)}
					<div class="plan-limit-paused">{PAUSED_AT_PLAN_LIMIT}</div>
				{/each}
			{/snippet}
		</WorkBlock>
	{/if}
{/each}

{#if renderedStreaming && !hasLiveFold}
	<LiveToolActivity {threadId} {expandedTools} onToggleToolBlock={onToggleToolBlock} />
{/if}

<style>
	.message {
		max-width: 760px;
		margin: 0 auto 20px;
		overflow-x: hidden;
	}
	.message.user {
		animation: fadeIn var(--dur-tight) ease;
		display: flex;
		justify-content: flex-end;
		align-items: flex-start;
	}
	.message.user.agent-inbound { justify-content: flex-start; }
	.message.assistant { animation: fadeInUp var(--dur-spring) ease; }
	.message.system { animation: fadeIn var(--dur-tight) ease; }
	@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
	@keyframes fadeInUp {
		from { opacity: 0; transform: translateY(8px); }
		to { opacity: 1; transform: translateY(0); }
	}
	.plan-limit-paused { max-width: 760px; margin: -6px auto 20px; color: var(--text-secondary); font-size: var(--text-footnote); }
	.historical-anchor { max-width: 760px; margin: 0 auto 8px; color: var(--text-secondary); font-size: var(--text-footnote); }
</style>
