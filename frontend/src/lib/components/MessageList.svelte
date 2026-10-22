<script lang="ts">
	import { tick } from 'svelte';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { loadDeferredToolResult } from '$lib/toolResultLoader';
	import { messages, activeThreadId, isThreadTurnActive } from '$lib/stores/threads';
	import { turnStates } from '$lib/ws/turnState';
	import EmptyChatState from './EmptyChatState.svelte';
	import MessageListBody from './MessageListBody.svelte';
	import { LOAD_OLDER_TRIGGER_PX, requestOlderForVisibleEntry, requestOlderIfTop } from '$lib/messagePager';
	import Button from '$lib/components/ui/Button.svelte';
	import OlderHistoryRow from './OlderHistoryRow.svelte';
	import { activeCheckpoint, checkpointsByThread, latestCheckpoint } from '$lib/stores/checkpoints';
	import { isAwaitingDelivery, isImminentDelivery } from '$lib/messageDelivery';
	import { buildMessageListPresentation, buildTranscriptPresentation } from './messageListModel';
	import { coreSettings } from '$lib/stores/settings';
	import { mergeTranscriptUpdates } from './transcriptUpdates';
	import { endVisit, loadUpdateCards, openAtDivider, threadUpdateCards, visitDividers, watchUpdateCardNudges } from '$lib/stores/threadUpdateCards';
	import { watchMessageCards } from '$lib/stores/messageCardRefresh';
	import { DividerLandingIntent, RenderedUpdateCards, scheduleDividerLanding } from './messageListUpdateCards';
	import { countVisibleEntries, visibleEntryContinuation } from './transcriptFocusModel';
	import { planLimitPauseDescriptors } from './planLimitPause';
	import { hasLandingTarget as hasLandingTargetIn, landOnNewContent, observeLayoutGrowth } from './messageListLayout';
	import { runHasMore, type RunLoad, type RunStub } from '$lib/focusRuns';
	import { needsWakeRetry } from './pendingWakeRetry';
	import { isHumanMessage } from './transcriptRowKinds';
	import { NEAR_BOTTOM_PX } from '$lib/nearBottom';
	import { createFocusWorkBlock } from './focusWorkBlock.svelte';

	let { messagesEl = $bindable(), userIsNearBottom = $bindable(true), ignoreScrollEvents = $bindable(false), onOpenLightbox, hasMoreMessages = false, loadingOlderMessages = false, olderLoadFailed = false, onLoadOlder, firstLoadPending = false, firstLoadFailed = false, onRetryFirstLoad, threadId = undefined, turnSummaries = {}, historyComplete = undefined, focusRuns = [], runLoads = {}, onLoadRun }: {
		messagesEl: HTMLDivElement | undefined;
		userIsNearBottom: boolean;
		ignoreScrollEvents: boolean;
		onOpenLightbox: (url: string) => void;
		hasMoreMessages?: boolean;
		loadingOlderMessages?: boolean;
		olderLoadFailed?: boolean;
		onLoadOlder?: (needsMore?: () => boolean) => void;
		firstLoadPending?: boolean;
		firstLoadFailed?: boolean;
		onRetryFirstLoad?: () => void;
		/** The thread to render; defaults to the active thread (#1949: a subagent leaf). */
		threadId?: string | null;
		turnSummaries?: Record<string, any>;
		/** Whether no older rows exist; an owner that does not page says so itself. */
		historyComplete?: boolean;
		/** Focus: the stubs of runs the first pages left unloaded, and where reading each stands. */
		focusRuns?: readonly RunStub[];
		runLoads?: Record<string, RunLoad>;
		onLoadRun?: (stub: RunStub) => void;
	} = $props();
	let expandedTools = $state(new Set<string>());
	let expandedReasoning = $state(new Set<string>());
	// The shared store can retain a prior selection; filter at this transcript boundary (#1084).
	const renderedThreadId = $derived(threadId ?? $activeThreadId);
	const messageRows = $derived($messages.filter((m) => m.thread_id === renderedThreadId));
	const renderedStreaming = $derived(isThreadTurnActive($turnStates, renderedThreadId));
	const focusLive = createFocusWorkBlock(() => renderedThreadId, () => focusMode);
	const checkpointState = $derived(
		renderedThreadId ? $checkpointsByThread.get(renderedThreadId) : undefined,
	);
	const currentCheckpoint = $derived(activeCheckpoint(checkpointState));
	const transcriptCheckpoint = $derived.by(() => {
		if (currentCheckpoint) return currentCheckpoint;
		const latest = latestCheckpoint(checkpointState);
		return latest?.status === 'failed' ? latest : null;
	});
	function toggled(set: Set<string>, id: string) {
		const next = new Set(set);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		return next;
	}
	const toggleToolBlock = (id: string) => { expandedTools = toggled(expandedTools, id); };
	const toggleReasoning = (id: string) => { expandedReasoning = toggled(expandedReasoning, id); };
	function loadToolResult(messageId: string, toolCallId: string): void {
		if (!renderedThreadId || !$token) return;
		void loadDeferredToolResult(renderedThreadId, messageId, toolCallId, $token);
	}
	const pauseDescriptors = $derived(planLimitPauseDescriptors(messageRows, turnSummaries));
	const presentation = $derived(buildMessageListPresentation(messageRows, pauseDescriptors.map((pause) => pause.source)));
	const focusMode = $derived($coreSettings?.features?.focus_mode === true);
	// G2: in Focus a wake that needs a Retry lives in the pending area, never inside a block.
	const transcriptMessages = $derived(presentation.visibleMessages.filter(
		(msg) => !isAwaitingDelivery(msg, renderedStreaming) && !(focusMode && needsWakeRetry(msg))));
	// Cards cover the loaded window: from its lowest message, or all once the first page is in.
	const cardsFromSeq = $derived.by(() => {
		if (focusMode || !hasMoreMessages) return 0; // Focus reads every card once; the window filters them
		const seqs = messageRows.map((m) => m.seq).filter((seq): seq is number => typeof seq === 'number');
		return seqs.length ? Math.min(...seqs) : 0;
	});
	const windowFloor = $derived.by(() => {
		if (!focusMode || !hasMoreMessages) return 0;
		const seqs = messageRows.map((m) => m.seq).filter((seq): seq is number => typeof seq === 'number');
		return Math.min(...seqs, ...focusRuns.map((run) => run.firstSeq));
	});
	// The divider is read once per open; the server freezes its pre-claim boundary.
	let dividerOpenFor: string | null = null;
	let landedVisit: string | null = null;
	$effect(() => {
		const id = renderedThreadId;
		const fromSeq = cardsFromSeq;
		watchUpdateCardNudges();
		watchMessageCards();
		if (!id) return;
		const opening = dividerOpenFor !== id;
		dividerOpenFor = id;
		if (opening) userIsNearBottom = true;
		void loadUpdateCards(id, fromSeq, { divider: opening });
	});
	$effect(() => {
		const id = renderedThreadId;
		return () => {
			if (!id) return;
			endVisit(id);
			if (dividerOpenFor === id) dividerOpenFor = null;
			cancelDividerLanding(id);
			if (landedVisit === id) landedVisit = null;
		};
	});
	const liveTurn = $derived(
		focusLive.current.active
			? { turnId: renderedThreadId ? $turnStates.get(renderedThreadId)?.turnId ?? null : null }
			: null,
	);
	const transcriptEntries = $derived(mergeTranscriptUpdates(
		buildTranscriptPresentation(transcriptMessages, focusMode, liveTurn, undefined,
			focusMode ? focusRuns.filter((run) => runHasMore(run, runLoads[run.lastId])) : []),
		(renderedThreadId ? $threadUpdateCards[renderedThreadId]?.cards ?? [] : []).filter((card) => card.after_seq >= windowFloor),
		{
			focusMode, divider: renderedThreadId ? $visitDividers[renderedThreadId]?.divider ?? null : null,
			pauses: pauseDescriptors, turnSummaries,
			sourceContext: renderedThreadId ? {
				threadId: renderedThreadId, phase: $turnStates.get(renderedThreadId)?.phase ?? 'idle',
				turnId: $turnStates.get(renderedThreadId)?.turnId ?? null,
			} : undefined,
		},
	));
	const dividerLanding = new DividerLandingIntent((threadId) => {
		landedVisit = threadId;
		if (get(openAtDivider) === threadId) openAtDivider.set(null);
	});
	const renderedCards = new RenderedUpdateCards();
	function cancelDividerLanding(threadId: string | null = renderedThreadId): void {
		if (!threadId) return;
		landedVisit = threadId;
		dividerLanding.cancel(threadId);
		if (get(openAtDivider) === threadId) openAtDivider.set(null);
	}
	$effect(() => {
		const id = renderedThreadId;
		const cards = id ? $threadUpdateCards[id]?.cards : undefined;
		if (!renderedCards.changed(id, cards) || !cards?.length || !userIsNearBottom
			|| $openAtDivider === id || (focusMode && landedVisit !== id && hasLandingTarget) || dividerLanding.blocksLiveEdge(id)) return;
		scrollToBottom();
	});
	const hasLandingTarget = $derived(hasLandingTargetIn(transcriptEntries));
	$effect(() => {
		const id = renderedThreadId;
		if (!id || landedVisit === id || firstLoadPending || !(id in $visitDividers) || (!focusMode && $openAtDivider !== id)
			|| dividerLanding.blocksLiveEdge(id)) return;
		if (!hasLandingTarget) { if (!$visitDividers[id].divider) landedVisit = id; openAtDivider.set(null); return; }
		scheduleDividerLanding(
			dividerLanding.begin(id), dividerLanding, tick, requestAnimationFrame,
			beginProgrammaticScroll, () => landOnNewContent(messagesEl),
		);
	});
	const pendingDeliveryMessages = $derived(presentation.visibleMessages.filter((msg) => isImminentDelivery(msg, renderedStreaming)));
	// G2: Focus shows your own waiting messages only; a context update that is just waiting is not shown.
	const pendingEntries = $derived([
		...(presentation.pendingContextDigest && !focusMode
			? buildTranscriptPresentation([presentation.pendingContextDigest], focusMode, null, 'run:pending-context') : []),
		...buildTranscriptPresentation(pendingDeliveryMessages.filter((msg) => !focusMode || isHumanMessage(msg)), focusMode),
	]);
	// Programmatic scrolling ends on any real gesture; never undo a user's reread.
	let pendingReleaseFrame: number | null = null;
	// A deferred pin abandons itself when a newer gesture takes over.
	let userScrollEpoch = 0;
	/** Read the live scroll position. Never assert where the user "should" be. */
	function syncNearBottom() {
		if (!messagesEl) return;
		const { scrollTop, scrollHeight, clientHeight } = messagesEl;
		userIsNearBottom = scrollHeight - scrollTop - clientHeight < NEAR_BOTTOM_PX;
	}
	function endProgrammaticScroll() {
		if (pendingReleaseFrame !== null) {
			cancelAnimationFrame(pendingReleaseFrame);
			pendingReleaseFrame = null;
		}
		ignoreScrollEvents = false;
	}
	/** A wheel, a finger, or an arrow key: the user is driving now. */
	function onUserScrollIntent() {
		userScrollEpoch++;
		cancelDividerLanding();
		endProgrammaticScroll();
		syncNearBottom();
	}
	function onWheel(e: WheelEvent) {
		onUserScrollIntent();
		if (e.deltaY < 0) loadOlderIfTopInView(); // no scroll event exists at scrollTop 0
	}
	function beginProgrammaticScroll(write: () => void) {
		if (!messagesEl) return;
		ignoreScrollEvents = true;
		write();
		if (pendingReleaseFrame !== null) cancelAnimationFrame(pendingReleaseFrame);
		pendingReleaseFrame = requestAnimationFrame(() => {
			pendingReleaseFrame = null;
			ignoreScrollEvents = false;
			// Re-read rather than claim: if the user moved during this frame, the
			// element already says so and that answer is the true one.
			syncNearBottom();
		});
	}
	function loadOlderIfTopInView(): boolean {
		if (!messagesEl || !onLoadOlder) return false;
		return requestOlderIfTop({ hasMore: hasMoreMessages, loading: loadingOlderMessages, error: olderLoadFailed }, messagesEl.scrollTop, loadOlderForVisibleEntry);
	}
	const loadOlderForVisibleEntry = () => onLoadOlder?.(focusMode ? visibleEntryContinuation(() => transcriptEntries) : undefined);
	function onMessagesScroll() {
		if (!messagesEl || ignoreScrollEvents) return;
		syncNearBottom();
		loadOlderIfTopInView();
	}
	let autoPaged = false;
	$effect(() => { void renderedThreadId; autoPaged = false; });
	$effect(() => {
		void [messageRows.length, hasMoreMessages, loadingOlderMessages, olderLoadFailed, focusMode, transcriptEntries];
		const el = messagesEl;
		if (!el || messageRows.length === 0 || autoPaged) return;
		tick().then(() => {
			const needsVisibleEntry = focusMode && countVisibleEntries(transcriptEntries) === 0;
			const requested = needsVisibleEntry ? requestOlderForVisibleEntry({ hasMore: hasMoreMessages, loading: loadingOlderMessages, error: olderLoadFailed }, loadOlderForVisibleEntry) : el.scrollHeight - el.clientHeight <= LOAD_OLDER_TRIGGER_PX && loadOlderIfTopInView();
			if (requested) autoPaged = true;
		});
	});
	/** Pin to the live edge after the DOM settles (new message, new step). */
	export function scrollToBottom() {
		if (dividerLanding.blocksLiveEdge(renderedThreadId)) return;
		const epoch = userScrollEpoch;
		tick().then(() => {
			// The gap between asking and writing belongs to the user too.
			if (epoch !== userScrollEpoch || dividerLanding.blocksLiveEdge(renderedThreadId)) return;
			followBottom();
		});
	}
	/** Pin to the live edge right now — for high-frequency reveal ticks. */
	export function followBottom() {
		if (dividerLanding.blocksLiveEdge(renderedThreadId)) return;
		beginProgrammaticScroll(() => {
			if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
		});
	}
	// Re-pin a live-edge transcript when the composer or attention rail grows; preserve a reader's history position.
	let wasPinnedBeforeLayoutChange = $state(true);
	$effect(() => {
		wasPinnedBeforeLayoutChange = userIsNearBottom;
	});
	$effect(() => observeLayoutGrowth(messagesEl, () => wasPinnedBeforeLayoutChange, followBottom));
	let savedBottomOffset = 0;
	export function captureBottomOffset() {
		if (!messagesEl) return;
		const { scrollTop, scrollHeight, clientHeight } = messagesEl;
		savedBottomOffset = Math.max(0, scrollHeight - scrollTop - clientHeight);
	}
	export function restoreBottomOffset() {
		beginProgrammaticScroll(() => {
			if (!messagesEl) return;
			const { scrollHeight, clientHeight } = messagesEl;
			if (savedBottomOffset === 0) {
				messagesEl.scrollTop = scrollHeight;
			} else {
				messagesEl.scrollTop = Math.max(0, scrollHeight - clientHeight - savedBottomOffset);
			}
		});
	}
</script>
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="messages"
	bind:this={messagesEl}
	onscroll={onMessagesScroll}
	onwheel={onWheel}
	ontouchmove={onUserScrollIntent}
	onpointerdown={onUserScrollIntent}
	onkeydown={onUserScrollIntent}
>
	<!--
		Three-way, because "this thread is empty" is a CLAIM about the thread and
		only one of these three states can support it. A first page still in
		flight, or one that failed, means the transcript is unknown — and after a
		failure the user needs the same way out the older-pages branch already
		offers, not a page reload.
	-->
	{#if firstLoadFailed}
		<div class="first-load-state first-load-error" class:full-canvas={messageRows.length === 0}>
			<span>Couldn't load this thread.</span>
			<Button size="sm" variant="secondary" ariaLabel="Retry loading this thread" onclick={() => onRetryFirstLoad?.()}>Retry</Button>
		</div>
	{:else if firstLoadPending && messageRows.length === 0}
		<div class="first-load-state full-canvas">
			<svg class="spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
			<span>Loading this thread…</span>
		</div>
	{:else if messageRows.length === 0 && !focusLive.current.active}
		<EmptyChatState />
	{/if}
	<OlderHistoryRow loading={loadingOlderMessages} failed={olderLoadFailed} hasMore={hasMoreMessages} hasRows={messageRows.length > 0} onLoadOlder={loadOlderForVisibleEntry} />
	<!-- Every transcript row, live work included, renders through the body's Focus classification. -->
		<MessageListBody
			{transcriptEntries}
			{pendingEntries}
			retryRows={focusMode ? messageRows.filter(needsWakeRetry) : []}
			{expandedTools}
			{expandedReasoning}
			onToggleToolBlock={toggleToolBlock}
			onToggleReasoning={toggleReasoning}
			onLoadToolResult={$token ? loadToolResult : undefined}
			{onOpenLightbox}
			threadId={renderedThreadId}
			{renderedStreaming}
			liveRollup={focusLive.current}
			checkpoint={transcriptCheckpoint}
			{turnSummaries}
			pauses={focusMode ? [] : pauseDescriptors}
			historyComplete={historyComplete ?? !(hasMoreMessages || firstLoadPending || firstLoadFailed)}
			{runLoads}
			{onLoadRun}
		/>
	</div>
{#if !userIsNearBottom && (focusLive.current.active || messageRows.length > 0)}
	<button class="scroll-fab" onclick={scrollToBottom} aria-label="Scroll to bottom" title="Scroll to bottom">
		<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12l7 7 7-7"/></svg>
	</button>
{/if}
<style>
	.first-load-state {
		display: flex; align-items: center; justify-content: center; gap: 8px;
		padding: 12px; color: var(--text-muted); font-size: var(--text-footnote);
	}
	.first-load-error { color: var(--red); }
	.first-load-state.full-canvas {
		height: 100%; flex-direction: column; justify-content: center;
	}
	/* Over a transcript that already has rows the notice must stay reachable:
	   at the top of a long scroll region it would be invisible exactly when it
	   matters. */
	.first-load-error:not(.full-canvas) {
		position: sticky; top: var(--sticky-top, 0px); z-index: 5;
		background: var(--glass-thin);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		border-radius: 8px;
	}
	/* Padding-top would be added to a sticky child's `top`, so the lead-in under the header is a spacer;
	   sticky chrome then pins at `--sticky-top`, 8px under the header. */
	.messages {
		flex: 1; overflow-y: auto;
		--sticky-top: calc(var(--header-bottom) + 8px);
		--messages-lead: calc(var(--header-bottom) + 24px);
		padding: 0 16px calc(var(--input-bar-height, 100px) + 16px);
	}
	.messages::before { content: ''; display: block; flex: none; height: var(--messages-lead); }
	@media (max-width: 768px) {
		.messages {
			--sticky-top: calc(var(--header-bottom) + 8px);
			--messages-lead: var(--messages-pad-top, calc(var(--header-bottom) + 24px));
			padding-left: 12px;
			padding-right: 12px;
			padding-bottom: calc(var(--input-bar-height, 100px) + var(--kb-toolbar-inset, 0px) + var(--empty-parallax-offset, 0px) + 16px);
			transform: translateY(var(--empty-parallax-offset, 0));
		}
	}
	/* Transcript row styling is owned by TranscriptContent. */
	.scroll-fab {
		position: absolute;
		bottom: calc(var(--input-bar-height, 100px) + 12px);
		left: 50%;
		transform: translateX(-50%);
		width: 36px; height: 36px; border-radius: var(--radius-circle);
		background: var(--glass-thin);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		border: 1px solid var(--border-subtle);
		color: var(--text-secondary); cursor: pointer;
		display: flex; align-items: center; justify-content: center;
		box-shadow: var(--glass-rim), 0 2px 8px rgba(0,0,0,0.15);
		z-index: 10; transition: background var(--dur-tight), color var(--dur-tight); animation: fadeIn var(--dur-tight) ease;
	}
	.scroll-fab:hover { background: var(--bg-hover); color: var(--text-primary); }
	:global(.spin) { animation: spin 1s linear infinite; }
	@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
	@media (max-width: 768px) {
		/* Mobile's live-edge control belongs to ChatAttentionStack's shared
		   host / scroll / Plan rail, which owns the keyboard inset once. */
		.scroll-fab { display: none; }
	}
</style>
