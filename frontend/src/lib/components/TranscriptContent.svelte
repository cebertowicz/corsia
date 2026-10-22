<script lang="ts">
	import UserMessageBubble from './UserMessageBubble.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import SystemMessageRow from './SystemMessageRow.svelte';
	import LiveToolActivity from './LiveToolActivity.svelte';
	import UpdateCard from './UpdateCard.svelte';
	import PassiveOutcomeList from './PassiveOutcomeList.svelte';
	import VisitDivider from './VisitDivider.svelte';
	import WorkBlock from './WorkBlock.svelte';
	import WaitCapsule from './WaitCapsule.svelte';
	import WaitLine from './WaitLine.svelte';
	import { waitFace } from './waitFace';
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import { coreSettings } from '$lib/stores/settings';
	import { activeToolsByThread, messages } from '$lib/stores/threads';
	import { INITIAL_TURN_STATE, turnStates } from '$lib/ws/turnState';
	import { threadLiveStep, UNKNOWN_STEP } from './plans/planLiveStep';
	import { createLiveLine } from './plans/liveLine.svelte';
	import type { TranscriptEntry } from './messageListModel';
	import { isRunExpanded, runTokens, toggleRun } from './transcriptFocusModel';
	import { liveRunTimes, runLabels, runRanges, waitElapsed, waitRange, type RunRange, type TurnSummaries } from './transcriptRunLabel';
	import { minuteClock } from '$lib/time';
	import { trackVisibleRow } from '$lib/actions/trackVisibleRow';
	import { openMilliseconds, openRangeKey, openTimes, requestOpenTime, type OpenTimeQuery } from '$lib/openTime';
	import { attachPlanLimitPauses, stubPauseTurns, type PlanLimitPauseDescriptor } from './planLimitPause';
	import { deliveryPresentation, isAwaitingDelivery } from '$lib/messageDelivery';
	import type { TranscriptMessage } from './messageListModel';
	import type { PauseAnnotation } from './transcriptFocusModel';
	import EventChip from './EventChip.svelte';
	import { sourceMembers } from './contextEventModel';
	import { PAUSED_AT_PLAN_LIMIT } from '$lib/stores/overageSetting';
	import type { RunLoad, RunStub } from '$lib/focusRuns';
	import type { WorkBlockLive } from './workBlockLive';

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
		liveRollup = null,
		showWait = true,
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
		/** Parent + descendant activity derived once at the MessageList boundary. */
		liveRollup?: WorkBlockLive | null;
		/** False for the pending-input copy, so the outside-wait face shows once. */
		showWait?: boolean;
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
		entry.kind === 'update' && entry.card.kind === 'blocked' && entry.card.ask?.hold_id
			? [entry.card.ask.hold_id] : [])));
	// Every ask card on screen: on its update row, or on a message the transcript shows (a collapsed run's are not).
	const renderedAsks = $derived.by(() => {
		const holds = new Set(homedHolds);
		renderedEntries.forEach((entry, index) => {
			const items = entry.kind === 'message' ? [entry] : entry.kind === 'fold'
				&& isRunExpanded(runTokens(renderedEntries, index, historyComplete), expandedFolds) ? entry.items : [];
			for (const item of items) for (const card of item.message.cards ?? []) if (card.hold_id) holds.add(card.hold_id);
		});
		return holds;
	});
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
	const ownLiveNow = $derived(
		boundThreadId ? threadLiveStep(boundThreadId, $turnStates, $activeToolsByThread) : null,
	);
	const liveNow = $derived(liveRollup?.step ?? ownLiveNow);
	const liveOwnThread = $derived(liveRollup?.ownRunning ?? ownLiveNow !== null);
	const liveActorName = $derived(liveRollup?.actorName ?? null);
	const livePreviewFromDescendant = $derived(liveRollup?.previewFromDescendant ?? false);
	const liveRunIndex = $derived(renderedEntries.findIndex((entry) => entry.kind === 'fold' && entry.live));
	const hasLiveFold = $derived(liveRunIndex !== -1);
	// The live line restarts after each shown item: its segment is the item before the open run.
	const liveSegment = $derived(hasLiveFold
		? runTokens(renderedEntries, liveRunIndex, historyComplete).find((token) => token.startsWith('after:')) ?? 'after:'
		: undefined);
	// A descendant can keep this Focus block live after the visible parent's turn settles.
	const liveLine = createLiveLine(() => liveNow, () => !(liveRollup?.active ?? renderedStreaming), () => liveSegment);
	// One server number, open time, for every run and for the wait chip (it counts on locally while the work is open).
	const runRangeList = $derived(runRanges(renderedEntries, turnSummaries, historyComplete));
	const openKey = (range: RunRange) => openRangeKey(boundThreadId ?? '', range);
	const openMs = (range: RunRange) => (boundThreadId ? openMilliseconds($openTimes, openKey(range), $minuteClock.getTime()) : null);
	const outsideWaitRange = $derived(waitRange(renderedEntries, turnSummaries, historyComplete));
	const labels = $derived(runLabels(renderedEntries, turnSummaries, historyComplete, openMs));
	// An outside wait with nothing running and no sent input waiting to start a turn (EW8). One predicate
	// gates both the Focus chip and the non-Focus face, so a turn starting swaps it out in the same render.
	// pendingInputIds covers only this tab's send before its first receipt; the transcript row is the server's
	// truth for an input it still holds (queued, or Queue-after-turn), after a reload or from another tab too.
	const inputAwaiting = $derived($messages.some((msg) => msg.thread_id === boundThreadId && isAwaitingDelivery(msg, false)));
	const focusMode = $derived($coreSettings?.features?.focus_mode === true);
	const waitState = $derived(boundThreadId ? $threadPlanStates[boundThreadId] : undefined);
	const wait = $derived(
		showWait && waitState?.state === 'waiting' && waitState.wait
			&& (boundTurn?.phase === 'idle' || boundTurn?.phase === 'terminal') && boundTurn.pendingInputIds.length === 0 && !inputAwaiting
			&& !renderedStreaming && !hasLiveFold
			? waitFace(waitState.wait, waitElapsed(outsideWaitRange, openMs), $minuteClock)
			: null,
	);
	// The open run is the trailing block; after a reply there is none, and the chip stands alone in its place.
	const waitRunId = $derived.by(() => {
		const last = renderedEntries.at(-1);
		return last?.kind === 'fold' ? last.id : null;
	});
	// A block a descendant keeps live counts its last Turn on to now, so the worker's time is included.
	const liveTimes = $derived(hasLiveFold ? liveRunTimes(renderedEntries, turnSummaries, historyComplete, openMs) : new Map<string, string | null>());
	// Ask the server again only when something that changes the number happens: a range, a Turn starting or ending
	// (this thread's or a descendant's), or the plan's wait. Never on a timer: the count between answers is local.
	const openQueries = $derived.by((): OpenTimeQuery[] => {
		if (!boundThreadId) return [];
		const ranges = outsideWaitRange ? [...runRangeList, outsideWaitRange] : runRangeList;
		return ranges.map((range) => ({ key: openKey(range), threadId: boundThreadId, since: range.since, until: range.until }));
	});
	const openTimeEvents = $derived(JSON.stringify([
		boundTurn?.phase, boundTurn?.turnId, liveRollup?.active ?? null, waitState?.state, waitState?.wait?.until ?? null,
		Object.entries(turnSummaries).map(([id, summary]) => `${id}:${summary.status}:${summary.ended_at}`),
	]));
	$effect(() => {
		void openTimeEvents;
		requestOpenTime(openQueries);
	});
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
	<div class="message {msg.role}" data-message-id={msg.id} use:trackVisibleRow={{ seq: msg.seq, threadId: boundThreadId }} class:agent-inbound={isAgentInbound} class:human-user={msg.role === 'user' && !isAgentInbound}>
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
				{renderedAsks}
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
			{liveOwnThread}
			{liveActorName}
			{livePreviewFromDescendant}
			load={entry.stub ? runLoads[entry.stub.lastId] : undefined}
			onToggle={() => { expandedFolds = toggleRun(tokens, expandedFolds); }}
			{onLoadRun}
			row={messageRow}
			pause={historicalPause}
			warning={deliveryWarning}
			wait={focusMode && entry.id === waitRunId ? wait : null}
			waitThreadId={boundThreadId}
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
{:else if wait && !(focusMode && waitRunId !== null)}
	{#if focusMode}
		<!-- No run to open: a static capsule, no toggle or chevron, keeping the label link. -->
		<section class="wait-standalone" data-work-block><WaitCapsule face={wait} threadId={boundThreadId} /></section>
	{:else}
		<WaitLine face={wait} threadId={boundThreadId} />
	{/if}
{/if}

<style>
	.wait-standalone { display: flex; max-width: var(--transcript-max-width); margin: 0 auto 14px; min-width: 0; }
	.message {
		max-width: var(--transcript-max-width);
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
	.plan-limit-paused { max-width: var(--transcript-max-width); margin: -6px auto 20px; color: var(--text-secondary); font-size: var(--text-footnote); }
	.historical-anchor { max-width: var(--transcript-max-width); margin: 0 auto 8px; color: var(--text-secondary); font-size: var(--text-footnote); }
</style>
