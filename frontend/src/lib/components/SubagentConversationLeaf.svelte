<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { on, subscribeThread, unsubscribeThread } from '$lib/ws';
	import { hydrateFetchedMessages } from '$lib/messageHistory';
	import { messages } from '$lib/stores/threads';
	import { adoptThreadSnapshot, claimDisplayedThread } from '$lib/stores/displayedThreads';
	import MessageList from './MessageList.svelte';
	import InputBar from './InputBar.svelte';
	import Button from '$lib/components/ui/Button.svelte';

	type TurnSummary = {
		started_at: string | null;
		ended_at: string | null;
		status: string;
		activity_count: number;
		cause?: string | null;
	};

	let {
		threadId,
		onOpenLightbox = () => {},
	}: {
		threadId: string;
		onOpenLightbox?: (url: string) => void;
	} = $props();

	// The leaf claims its thread in the shared message store (#1949), so every
	// delivery writer updates it exactly as it updates the active thread.
	let messagesEl = $state<HTMLDivElement | undefined>();
	let userIsNearBottom = $state(true);
	let ignoreScrollEvents = $state(false);
	let loading = $state(true);
	let failed = $state(false);
	let generation = 0;
	let turnSummaries = $state<Record<string, TurnSummary>>({});
	// The leaf loads one page and never pages, so the response says whether older rows exist.
	let historyComplete = $state(false);
	let messageListRef = $state<MessageList | undefined>();

	const hasRows = $derived($messages.some((row) => row.thread_id === threadId));

	function scrollToBottom(): void {
		messageListRef?.scrollToBottom();
	}

	async function load(): Promise<void> {
		const authToken = $token;
		const requestGeneration = ++generation;
		turnSummaries = {};
		historyComplete = false;
		if (!authToken) {
			loading = false;
			failed = true;
			return;
		}
		try {
			const query = new URLSearchParams({ token: authToken, limit: '50' });
			const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages?${query}`);
			if (!response.ok) throw new Error(`Child transcript load failed (${response.status})`);
			const body = await response.json();
			if (requestGeneration !== generation) return;
			turnSummaries = body.turn_summaries && typeof body.turn_summaries === 'object'
				? body.turn_summaries as Record<string, TurnSummary>
				: {};
			historyComplete = body.has_more === false;
			adoptThreadSnapshot(threadId, hydrateFetchedMessages(body.messages ?? []));
			failed = false;
		} catch {
			if (requestGeneration === generation) failed = true;
		} finally {
			if (requestGeneration === generation) loading = false;
		}
	}

	onMount(() => {
		// Teardown releases the thread this leaf subscribed, read once here: the
		// prop is a getter on the active tab, which is already gone by the time
		// a closed leaf unmounts.
		const subscribed = threadId;
		const release = claimDisplayedThread(subscribed);
		void load();
		subscribeThread(subscribed);
		const offConnected = on('connected', () => {
			subscribeThread(subscribed);
			void load();
		});
		const refreshLifecycle = (data: any) => {
			if (data?.thread_id !== subscribed || data?.error_type === 'stop_failed') return;
			void load();
		};
		const offActivity = on('turn_activity', refreshLifecycle);
		const offComplete = on('turn_complete', refreshLifecycle);
		const offCancelled = on('turn_cancelled', refreshLifecycle);
		const offInterrupted = on('turn_interrupted', refreshLifecycle);
		const offError = on('error', refreshLifecycle);
		const handleVisibilityChange = () => {
			if (document.visibilityState === 'visible') void load();
		};
		document.addEventListener('visibilitychange', handleVisibilityChange);
		return () => {
			generation += 1;
			unsubscribeThread(subscribed);
			offConnected?.();
			offActivity?.();
			offComplete?.();
			offCancelled?.();
			offInterrupted?.();
			offError?.();
			document.removeEventListener('visibilitychange', handleVisibilityChange);
			release();
		};
	});
</script>

<div class="subagent-conversation" data-drop-composer-surface aria-label="Subagent conversation">
	{#if failed && !hasRows}
		<div class="leaf-state" role="alert">
			<strong>This subagent conversation couldn’t load.</strong>
			<Button size="sm" variant="secondary" onclick={() => load()}>Retry</Button>
		</div>
	{:else}
		<MessageList
			bind:this={messageListRef}
			bind:messagesEl
			bind:userIsNearBottom
			bind:ignoreScrollEvents
			{threadId}
			{turnSummaries}
			{historyComplete}
			firstLoadPending={loading}
			firstLoadFailed={failed}
			onRetryFirstLoad={load}
			{onOpenLightbox}
		/>
		<InputBar
			{threadId}
			onSend={scrollToBottom}
		/>
	{/if}
</div>

<style>
	.subagent-conversation {
		display: flex; min-width: 0; min-height: 0; height: 100%; position: relative;
	}
	.subagent-conversation :global(.messages) {
		padding-top: var(--space-4);
	}
	.leaf-state {
		display: flex; flex: 1; flex-direction: column; align-items: center; justify-content: center;
		gap: var(--space-3); padding: var(--space-4); color: var(--text-secondary); text-align: center;
	}
</style>
