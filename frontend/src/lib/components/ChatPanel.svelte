<script lang="ts">
	import { onDestroy, tick } from 'svelte';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { threads, activeThreadId, activeThread, messages, isStreaming, reasoningEffort } from '$lib/stores/threads';
	import { activeModelId, bindActiveThreadModel } from '$lib/stores/models';
	import { corsiaAgentId } from '$lib/utils/conductor';
	import ConductorSourcesPanel from './ConductorSourcesPanel.svelte';
	import { loadCheckpoints } from '$lib/stores/checkpoints';
	import { beginThreadEntry } from '$lib/stores/contextUsageByThread';
	import { hydrateSubagentThread, subagentMeta } from '$lib/stores/subagents';
	import { clearWarmKeysForThread, warmActiveThread } from '$lib/chatWarm';
	import { subscribeChatPanelEvents } from './chatPanelEvents';
	import { startPresence, stopPresence } from '$lib/ws/presence';
	import { hydrateFetchedMessages, mergeFetchedHistoryRows } from '$lib/messageHistory';
	import { adoptThreadSnapshot, retainDisplayedRows } from '$lib/stores/displayedThreads';
	import { beginThreadOpen, failThreadOpen, markFirstPageRendered, type ThreadOpenSource } from '$lib/openTimingRuntime';
	import MessageList from './MessageList.svelte';
	import { coreSettings } from '$lib/stores/settings';
	import { adoptFirstPageRuns, adoptOlderPageRuns, type RunLoad, type RunStub } from '$lib/focusRuns';
	import { loadRunRange } from '$lib/focusRunLoader';
	import InputBar from './InputBar.svelte';
	import ChatDropOverlay from './ChatDropOverlay.svelte';
	import ChatWarmToast from './ChatWarmToast.svelte';
	import HostAccessLeaseControl from './HostAccessLeaseControl.svelte';
	import MobileScrollFab from './MobileScrollFab.svelte';
	import AttentionDock from './attention/AttentionDock.svelte';
	import ChatAttentionStack from './attention/ChatAttentionStack.svelte';
	import { toastAnchorSurface } from '$lib/stores/toastAnchor';
	import PlanDrawerControl from './plans/PlanDrawerControl.svelte';
	import { isMobile } from '$lib/stores/viewport';
	import { openPlanSubagentLeaf, openPlanDiscussionLeaf } from '$lib/planLeafReturn';
	import {
		createPager,
		adoptFirstPage,
		loadOlder,
		makeGuardedPort,
		supersedingRead,
		PAGER_ABORT,
		type MessagePage,
		type TurnSummary,
		type PagerPort,
	} from '$lib/messagePager';
	let {
		isDraggingOver = false,
		onOpenLightbox,
		onAutoCreateThread,
		onOpenDocuments = () => {},
	}: {
		isDraggingOver?: boolean;
		onOpenLightbox: (url: string) => void;
		/** Where an external review in the attention dock hands off. */
		onOpenDocuments?: (documentId?: string) => void;
		onAutoCreateThread?: () => Promise<void>;
	} = $props();
	let messagesEl = $state<HTMLDivElement | undefined>(undefined);
	let userIsNearBottom = $state(true);
	let ignoreScrollEvents = $state(false);
	let messageListRef: MessageList;
	let inputBarRef: InputBar;
	/**
	 * Precedence for the shared attention slot. `leaseAwaitingUser` is written
	 * only by the mounted `HostAccessLeaseControl`; gating it on the thread too
	 * means an unmounted control can never suppress the dock, and a new thread
	 * never inherits the previous thread's pending lease.
	 */
	let leaseAwaitingUser = $state(false);
	const attentionSuppressed = $derived(!!$activeThreadId && leaseAwaitingUser);
	const MSG_PAGE_SIZE = 50;
	let pager = $state(createPager());
	let pagerGeneration = 0;
	let firstLoadFailed = $state(false);
	// Keep first-page in-flight truth visible to the transcript.
	let firstLoadPending = $state(false);
	let lastFirstPageThreadId: string | null = null;
	// Page summaries are keyed by durable Turn identity; a later page refreshes
	// the prior value for that same Turn without disturbing other loaded Turns.
	let turnSummaries = $state<Record<string, TurnSummary>>({});
	// Focus: stubs of runs the pages left unloaded, and where reading each stands.
	let focusRuns = $state<RunStub[]>([]), runLoads = $state<Record<string, RunLoad>>({});
	const focusOn = () => $coreSettings?.features?.focus_mode === true;
	const focusParam = () => (focusOn() ? '&view=focus' : '');
	function mergeTurnSummaries(value: unknown): void {
		if (!value || typeof value !== 'object' || Array.isArray(value)) return;
		turnSummaries = { ...turnSummaries, ...(value as Record<string, TurnSummary>) };
	}

	const hasMoreMessages = $derived(!!$activeThreadId && pager.hasMore); // New chat has no history to page
	const loadingOlderMessages = $derived(pager.loading);
	const olderLoadFailed = $derived(pager.error);
	export const scrollToBottom = () => messageListRef?.scrollToBottom();
	function openPlanSubagent(nodeId: string, childThreadId: string, title: string, stepId?: string): void {
		openPlanSubagentLeaf($activeThreadId, nodeId, childThreadId, title, stepId);
	}
	function openPlanDiscussion(artifactId: string, title: string): void {
		openPlanDiscussionLeaf($activeThreadId, artifactId, title);
	}
	export const captureBottomOffset = () => messageListRef?.captureBottomOffset();
	export const restoreBottomOffset = () => messageListRef?.restoreBottomOffset();
	/** Warm the active thread's cache. Lives in `$lib/chatWarm`. */
	export const triggerWarm = warmActiveThread;
	export async function selectThread(
		id: string,
		source: ThreadOpenSource = 'other',
		firstRunOwner?: () => boolean,
	) {
		const ownsSelection = () => !firstRunOwner || firstRunOwner();
		if (!ownsSelection()) return;
		// Only a changed displayed thread is an open. Re-selecting what is already on
		// screen contributes a ~0 ms sample and would bias the percentiles this feeds.
		// Idempotent: the route already began this open before its awaits (#2280).
		if (get(activeThreadId) !== id) beginThreadOpen(id, source);
		pagerGeneration++;
		pager = createPager();
		turnSummaries = {};
		focusRuns = [];
		runLoads = {};

		const myGen = pagerGeneration;
		const ws = await import('$lib/ws');
		if (!ownsSelection()) return;
		const previous = get(activeThreadId);
		if (previous && previous !== id) {
			ws.unsubscribeThread(previous);
			ws.cancelThreadTypewriters(previous);
		}
		if (!ownsSelection()) return;
		activeThreadId.set(id);
		beginThreadEntry(id);
		ws.subscribeThread(id);
		const authToken = get(token);
		// Whether this thread IS a subagent is what routes a send to the child's
		// stdin channel and keeps Send live. A WS frame carries it on subscribe,
		// but a socket that has not opened yet carries nothing — so read it.
		const subagentRefresh = authToken
			? hydrateSubagentThread(id, authToken).catch(() => null)
			: Promise.resolve(null);
		try {
			await Promise.all([
				loadFirstPage(id, myGen),
				subagentRefresh,
			]);
		} catch (e) {
			console.error('selectThread fetch failed:', e);
		}
		if (!isCurrentFirstPageRequest(id, myGen)) return;
		scrollToBottom();
		const thread = $threads.find(s => s.id === id);
		if (thread) reasoningEffort.set(thread.reasoning_effort ?? null);
	}
	export async function fetchThreadMessages(threadId: string) {
		pagerGeneration++;
		pager = createPager();
		// A same-thread refresh replaces the first page but retains older rows, so
		// retain their keyed summaries until that page is actually unloaded.
		if (lastFirstPageThreadId !== null && lastFirstPageThreadId !== threadId) turnSummaries = {};
		const myGen = pagerGeneration;
		try {
			await loadFirstPage(threadId, myGen);
		} catch (e) {
			console.error('fetchThreadMessages fetch failed:', e);
		}
	}
	export async function setAndSend(text: string) {
		await inputBarRef?.setAndSend(text);
	}
	const pagerPort: PagerPort = {
		async fetchPage(cursor: string): Promise<MessagePage | null> {
			const t = $token;
			const sid = $activeThreadId;
			if (!t || !sid) return null;
			try {
				const res = await fetch(
					`/api/threads/${sid}/messages?token=${t}&limit=${MSG_PAGE_SIZE}&before=${cursor}${focusParam()}`
				);
				if (!res.ok) return null;
				const data = await res.json() as MessagePage;
				return { ...data, messages: hydrateFetchedMessages(data.messages ?? []) };
			} catch (e) {
				console.error('loadOlderMessages fetch failed:', e);
				return null;
			}
		},
		prepend(older: any[]) {
			messages.update(current => mergeFetchedHistoryRows(current, older));
		},
		getScrollEl: () => messagesEl ?? null,
		afterRender: () => tick(),
	};
	async function loadOlderMessages(needsMore?: () => boolean) {
		const gen = pagerGeneration;
		const guardedPort = makeGuardedPort(() => pagerGeneration, gen, pagerPort);
		await loadOlder(pager, {
			...guardedPort,
			async fetchPage(cursor) {
				const page = await guardedPort.fetchPage(cursor);
				if (page !== PAGER_ABORT && page) {
					mergeTurnSummaries((page as MessagePage).turn_summaries);
					focusRuns = adoptOlderPageRuns(focusRuns, page);
				}
				return page;
			},
		}, focusOn() ? 1 : undefined, needsMore); // a Focus page is one request, never a chain
	}
	const loadRun = (stub: RunStub) => void loadRunRange(stub, {
		pageSize: MSG_PAGE_SIZE, loads: () => runLoads, onSummaries: mergeTurnSummaries,
		setLoad: (key, load) => { runLoads = { ...runLoads, [key]: load }; },
	});
	let focusSeen: boolean | undefined; // a changed Focus setting changes what a page holds: start over
	$effect(() => {
		const changed = focusSeen !== undefined && focusSeen !== focusOn();
		focusSeen = focusOn();
		const id = get(activeThreadId);
		if (!changed || !id) return;
		[focusRuns, runLoads] = [[], {}];
		messages.update((current) => current.filter((row) => row.thread_id !== id));
		void fetchThreadMessages(id);
	});
	function isCurrentFirstPageRequest(threadId: string, gen: number): boolean {
		return pagerGeneration === gen && get(activeThreadId) === threadId;
	}
	// A newer first-page read makes the older one stale: abort it, never download both.
	const firstPageRead = supersedingRead();
	onDestroy(firstPageRead.abort);
	/** Drop stale rows when the selected transcript's first page fails. */
	function failFirstPage(threadId: string): void {
		firstLoadFailed = true;
		retainDisplayedRows(threadId);
		failThreadOpen(threadId);
	}
	async function loadFirstPage(threadId: string, gen: number): Promise<void> {
		const t = $token;
		if (!t) return;
		const mine = isCurrentFirstPageRequest(threadId, gen);
		if (mine) {
			lastFirstPageThreadId = threadId;
			firstLoadFailed = false;
			firstLoadPending = true;
		}
		const signal = firstPageRead.start();
		try {
			const res = await fetch(`/api/threads/${threadId}/messages?token=${t}&limit=${MSG_PAGE_SIZE}${focusParam()}`, { signal });
			if (!res.ok) {
				console.error(`loadFirstPage ${res.status} for thread ${threadId}`);
				if (isCurrentFirstPageRequest(threadId, gen)) failFirstPage(threadId);
				return;
			}
			const data = await res.json() as MessagePage;
			if (!isCurrentFirstPageRequest(threadId, gen)) return;
			firstLoadFailed = false;
			const hydrated = hydrateFetchedMessages(data.messages ?? []);
			mergeTurnSummaries(data.turn_summaries);
			adoptThreadSnapshot(threadId, hydrated);
			adoptFirstPage(pager, data);
			focusRuns = adoptFirstPageRuns(focusRuns, data, hydrated);
			await tick();
			// The first page is in the DOM now; this is what the user waits for.
			if (isCurrentFirstPageRequest(threadId, gen)) markFirstPageRendered(threadId, hydrated.length);
		} catch (e) {
			if (signal.aborted) return; // replaced by a newer read, not a failure on screen
			console.error(`loadFirstPage network error for thread ${threadId}:`, e);
			if (isCurrentFirstPageRequest(threadId, gen)) failFirstPage(threadId);
		} finally {
			if (isCurrentFirstPageRequest(threadId, gen)) firstLoadPending = false;
		}
	}
	/** Retry the first page of the thread on screen — the exit from a failed load. */
	async function retryFirstPage(): Promise<void> {
		const sid = get(activeThreadId) ?? lastFirstPageThreadId;
		if (!sid) return;
		await fetchThreadMessages(sid);
	}

	// The main panel is the sole view that reports its displayed thread.
	$effect(() => {
		const threadId = $activeThreadId;
		if (threadId) startPresence(threadId);
		else stopPresence();
	});
	onDestroy(stopPresence);

	// Every navigation path binds the composer to the opened thread's saved model.
	$effect(() => {
		const id = $activeThreadId;
		if (id) bindActiveThreadModel(id, $threads, $subagentMeta.get(id)?.model);
	});
	$effect(() => {
		if ($activeThreadId && $token)
			void loadCheckpoints($activeThreadId, $token).catch(() => null);
	});
	$effect(() => {
		const unsubscribeEvents = subscribeChatPanelEvents({
			activeThreadId,
			isNearBottom: () => userIsNearBottom,
			scrollToBottom,
			followBottom: () => messageListRef?.followBottom(),
			fetchThreadMessages,
			clearWarmKeys: clearWarmKeysForThread,
		});
		const handleVisibilityChange = async () => {
			if (document.visibilityState !== 'visible') return;
			const sid = get(activeThreadId);
			if (!sid) return;
			pagerGeneration++;
			pager = createPager();
			const myGen = pagerGeneration;
			const authToken = get(token);
			try {
				await Promise.all([
					loadFirstPage(sid, myGen),
					authToken
						? loadCheckpoints(sid, authToken).catch(() => null)
						: Promise.resolve(null),
					authToken
						? hydrateSubagentThread(sid, authToken).catch(() => null)
						: Promise.resolve(null),
				]);
			} catch { /* ignore */ }
		};
		document.addEventListener('visibilitychange', handleVisibilityChange);
		return () => {
			unsubscribeEvents();
			document.removeEventListener('visibilitychange', handleVisibilityChange);
		};
	});
</script>
<div class="chat-shell">
	<div class="chat-content" use:toastAnchorSurface>
		<MessageList
			bind:this={messageListRef}
			bind:messagesEl
			bind:userIsNearBottom
			bind:ignoreScrollEvents
			onOpenLightbox={(url) => onOpenLightbox(url)}
			{hasMoreMessages}
			{loadingOlderMessages}
			{olderLoadFailed}
			onLoadOlder={loadOlderMessages}
			{firstLoadPending}
			{firstLoadFailed}
			{turnSummaries}
			{focusRuns}
			{runLoads}
			onLoadRun={loadRun}
			onRetryFirstLoad={retryFirstPage}
		/>
		{#if isDraggingOver}
			<ChatDropOverlay />
		{/if}
		<ChatWarmToast />
		<!--
			The stack owns the one normal-flow bottom band. The lease comes first so
			its decision cannot be obscured by the dock or mobile controls.
		-->
		<ChatAttentionStack>
			{#if $activeThreadId && $activeThreadId === $corsiaAgentId && !$activeModelId}
				<ConductorSourcesPanel threadId={$activeThreadId} />
			{/if}
			{#if $activeThreadId}
				<HostAccessLeaseControl
					threadId={$activeThreadId}
					stacked
					bind:awaitingUser={leaseAwaitingUser}
				/>
			{/if}
			<AttentionDock
				suppressed={attentionSuppressed}
				onOpenDestination={(destination) => {
					if (destination.pane === 'documents') onOpenDocuments(destination.id);
				}}
			/>
			<MobileScrollFab
				threadId={$activeThreadId}
				{userIsNearBottom}
				onScrollToBottom={scrollToBottom}
			/>
			{#if $isMobile && $activeThreadId}
				<PlanDrawerControl
					threadId={$activeThreadId}
					onOpenChild={openPlanSubagent}
					onOpenLegacyDiscussion={openPlanDiscussion}
				/>
			{/if}
		</ChatAttentionStack>
		<InputBar
			bind:this={inputBarRef}
			onSend={scrollToBottom}
			onTriggerWarm={triggerWarm}
			{onAutoCreateThread}
		/>
	</div>
</div>
<style>
	.chat-shell {
		flex: 1; display: flex; min-height: 0; overflow: hidden;
	}
	.chat-content {
		flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; position: relative;
	}
</style>
