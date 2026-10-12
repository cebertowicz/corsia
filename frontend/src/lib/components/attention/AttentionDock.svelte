<script lang="ts">
	/**
	 * The one place "this needs you" is answered.
	 *
	 * Mounted once in the thread chat attention slot above the composer. State
	 * that must survive a transient remount lives in `$lib/stores/attentionDock`.
	 *
	 * Not mounted in documents or settings: those are transient modes the user
	 * entered deliberately, and the external-review destination *is* the
	 * documents pane — a row there pointing at the pane you are already in would
	 * duplicate its own renderer.
	 *
	 * `suppressed` defers to a higher-precedence control in the same physical
	 * slot (a host-access lease awaiting the user). Deferred, never hidden.
	 */
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { on } from '$lib/ws';
	import { connectionState } from '$lib/stores/connection';
	import { token } from '$lib/stores/auth';
	import { emitProductEvent } from '$lib/product_events';
	import { forgetAutoOpen, holdsDraft, judgeAutoOpen, noteLoad } from '$lib/stores/attentionAutoOpen';
	import { activeThreadId } from '$lib/stores/threads';
	import DecisionChip from './DecisionChip.svelte';
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import ExternalReviewRow from './ExternalReviewRow.svelte';
	import {
		collapse,
		EMPTY,
		expand,
		expandedId,
		activeId,
		focusRequest,
		focusTarget,
		queue,
		reload,
		shownItem,
		skip,
		stale,
		visibleQueue,
	} from '$lib/stores/attentionDock.svelte';
	import { answerNotice, answers } from '$lib/stores/attentionAnswerState';
	import type { DecisionDetail, ExternalReviewDetail } from '$lib/api/attentionQueue';

	let {
		writesDisabled = false,
		suppressed = false,
		onOpenDestination = () => {},
	}: {
		writesDisabled?: boolean;
		/** A higher-precedence control holds this slot right now. */
		suppressed?: boolean;
		onOpenDestination?: (destination: { pane: string; id: string }) => void;
	} = $props();

	let openTrigger = $state<HTMLButtonElement | null>(null);
	let region = $state<HTMLElement | null>(null);
	let servedFocusRequest = 0;
	/** How long after the last keydown the person still counts as typing. */
	const TYPING_IDLE_MS = 2000;
	let lastKeydown = 0;
	/** The element focused before an automatic open, so closing hands focus back. */
	let priorFocus: HTMLElement | null = null;
	/** Bumped when focus moves or the typing window ends, so a waiting arrival is re-judged. */
	let busyTick = $state(0);

	const current = $derived(shownItem($visibleQueue, $activeId));
	const position = $derived(
		current ? $visibleQueue.items.findIndex((i) => i.external_id === current.external_id) + 1 : 0,
	);
	const canSkip = $derived($visibleQueue.items.length > 1);
	const effectiveWritesDisabled = $derived(
		writesDisabled || $stale || $connectionState !== 'connected',
	);
	/** Answers shown as recorded whose write is still in flight. */
	const recording = $derived(
		Object.values($answers).some((answer) => answer.state === 'sending'),
	);
	const failed = $derived(
		current ? ($answers[current.external_id]?.state === 'failed') : false,
	);
	const showChip = $derived(
		!!current && !suppressed && $expandedId === current.external_id,
	);

	/** Focus returns to the exact control that opened the chip. */
	async function close() {
		collapse();
		await Promise.resolve();
		const back = priorFocus?.isConnected ? priorFocus : openTrigger;
		priorFocus = null;
		back?.focus();
	}


	const deferredId = 'attention-dock-deferred';

	/**
	 * The landmark's name carries the state a sighted user reads off the bar, so
	 * assistive tech gains it on arrival rather than after exploring.
	 */
	const regionLabel = $derived(
		!current
			? 'Needs you'
			: $visibleQueue.count > 1
				? `Needs you — ${label(current)}, ${$visibleQueue.count} waiting`
				: `Needs you — ${label(current)}`,
	);

	function open() {
		if (!current || suppressed) return;
		expand(current, suppressed);
	}

	$effect(() => {
		// Suppression closes rather than hides, so lifting it never reveals a
		// surface the user did not just ask for.
		if (suppressed && $expandedId) collapse();
	});

	$effect(() => {
		// A plan's blocking indicator asked for the canonical chip. Open it here:
		// one owner, no second instance, no navigation.
		if ($focusRequest === servedFocusRequest) return;
		servedFocusRequest = $focusRequest;
		if (!current || suppressed) return;
		expand(current, suppressed);
	});

	$effect(() => {
		// Activity opened this thread for its head decision. Consume the request
		// exactly once; a request for any other thread is dropped, never kept.
		const target = $focusTarget;
		if (!target) return;
		focusTarget.set(null);
		if (target !== $activeThreadId || current?.detail.source?.thread_id !== target) return;
		expand(current, suppressed);
	});

	$effect(() => {
		// A decision raised by the viewed thread after the last one judged opens by
		// itself once the person is not typing and nothing else holds the slot (see
		// the store). Busy is read from the document here, never stored.
		void busyTick;
		const typingLeft = lastKeydown + TYPING_IDLE_MS - Date.now();
		const outcome = judgeAutoOpen($visibleQueue.items, {
			viewedThreadId: $activeThreadId,
			expandedId: $expandedId,
			suppressed,
			busy: typingLeft > 0 || holdsDraft(document.activeElement),
		});
		if (outcome.state === 'open') {
			const focused = document.activeElement;
			priorFocus = focused instanceof HTMLElement && focused !== document.body ? focused : null;
			// The arrival becomes the shown item even when it is not the queue head.
			activeId.set(outcome.item.external_id);
			expand(outcome.item, false);
			emitProductEvent('attention_dock', 'decision_auto_opened', { thread_id: $activeThreadId });
			return;
		}
		if (outcome.state !== 'wait' || typingLeft <= 0) return;
		const timer = setTimeout(() => { busyTick += 1; }, typingLeft);
		return () => clearTimeout(timer);
	});

	let firstThread = true;
	$effect(() => {
		// The queue is global, so a thread coming into view is anchored from the
		// queue already held here (kept fresh while mounted); no fetch. The first
		// run is the mount, whose own load anchors it. Before any load has landed
		// for this identity the queue is still EMPTY, and an empty queue means
		// "nothing loaded", not "no decisions": leave the view unset so the first
		// real load anchors it.
		const threadId = $activeThreadId;
		if (firstThread) { firstThread = false; return; }
		const held = get(queue);
		if (held === EMPTY) forgetAutoOpen();
		else noteLoad(held.items, threadId);
	});

	onMount(() => {
		void reload();
		// Reuses the existing invalidation signal rather than inventing one; a
		// missed nudge is recovered on reconnect.
		const offNudge = on('plans_changed', () => { void reload(); });
		// Typing, pasting or sending ends in a re-judge once the window has passed,
		// so a hold on a draft is released when the draft is sent or cleared.
		let idleTimer: ReturnType<typeof setTimeout> | undefined;
		const noteActivity = () => {
			lastKeydown = Date.now();
			clearTimeout(idleTimer);
			idleTimer = setTimeout(() => { busyTick += 1; }, TYPING_IDLE_MS);
		};
		const rejudge = () => { setTimeout(() => { busyTick += 1; }, 0); };
		document.addEventListener('keydown', noteActivity, true);
		document.addEventListener('input', noteActivity, true);
		document.addEventListener('focusin', rejudge);
		document.addEventListener('focusout', rejudge);
		let wasConnected = false;
		const offConnection = connectionState.subscribe((state) => {
			const connected = state === 'connected';
			if (connected && !wasConnected) void reload();
			wasConnected = connected;
		});
		// The store drops the previous identity's queue wherever it happens; only
		// a mounted dock should go and fetch the new one, so signing in with
		// nothing on screen costs no request.
		let lastToken = get(token);
		const offToken = token.subscribe((value) => {
			if (value === lastToken) return;
			lastToken = value;
			if (value) void reload();
		});
		return () => {
			clearTimeout(idleTimer);
			document.removeEventListener('keydown', noteActivity, true);
			document.removeEventListener('input', noteActivity, true);
			document.removeEventListener('focusin', rejudge);
			document.removeEventListener('focusout', rejudge);
			forgetAutoOpen();
			offNudge?.();
			offConnection();
			offToken();
		};
	});

	function label(item: NonNullable<typeof current>): string {
		return item.renderer === 'decision'
			? (item.detail as DecisionDetail).title
			: (item.detail as ExternalReviewDetail).title;
	}
</script>

{#if recording}
	<!-- The answer is already out of the queue; this is its receipt until the
	     write confirms, and a failed write returns the decision with its reason. -->
	<p class="dock-receipt" role="status">Answer recorded</p>
{:else if $answerNotice}
	<!-- The question left the queue before this answer reached it. -->
	<p class="dock-receipt" role="status">{$answerNotice}</p>
{/if}
{#if current}
	<div
		class="attention-dock"
		class:expanded={showChip}
		data-attention-dock
		bind:this={region}
		role="region"
		aria-label={regionLabel}
	>
		<p class="sr-only" aria-live="polite">{regionLabel}</p>
		{#if !showChip}
			<div class="dock-row">
			<button
				class="dock-bar"
				bind:this={openTrigger}
				onclick={open}
				aria-disabled={suppressed}
				aria-expanded={showChip}
				aria-describedby={suppressed ? deferredId : undefined}
			>
				<span class="dock-mark" aria-hidden="true"></span>
				<span class="dock-text">
					<b>{label(current)}</b>
					{#if current.detail.source}<small>{current.detail.source.thread_label}</small>{/if}
				</span>
				{#if $visibleQueue.count > 1}
					<span class="dock-count">{position} of {$visibleQueue.count}</span>
				{/if}
				<span class="dock-action">
					{failed ? 'Not saved — Retry' : current.renderer === 'decision' ? 'Answer' : 'Review'} →
				</span>
			</button>
			</div>
		{/if}
		{#if current.renderer === 'decision'}
			{#key current.external_id}
				<AdaptiveDrawer
					ariaLabel={`Decision — ${label(current)}`}
					open={showChip}
					onClose={close}
				>
					<!-- No chrome heading: the decision's own header already carries a
					     "Decision · thread · project" eyebrow and the decision title. -->

					<DecisionChip
						decision={current.detail as DecisionDetail}
						writesDisabled={effectiveWritesDisabled}
						onSkip={canSkip ? skip : undefined}
					/>
				</AdaptiveDrawer>
			{/key}
		{:else if showChip}
			{#key current.external_id}
				<ExternalReviewRow
					review={current.detail as ExternalReviewDetail}
					onClose={close}
					onSkip={canSkip ? skip : undefined}
					{onOpenDestination}
				/>
			{/key}
		{/if}
		{#if suppressed && !showChip}
			<!-- Deferred, not hidden: say why, and keep the control focusable so a
			     keyboard or screen-reader user is not silently skipped past it. -->
			<p class="dock-deferred" id={deferredId}>
				Answer this after the host-access request above.
			</p>
		{/if}
		{#if $stale}
			<p class="dock-stale" role="status">Showing last known items.</p>
		{/if}
	</div>
{/if}

<style>
	.attention-dock {
		flex: none;
		padding: var(--space-2) var(--space-4) 0;
		/* The chat slot publishes this height budget so an opened chip scrolls
		   inside it instead of growing off the top of the viewport (measured at
		   320px: heading and close button at y = -91). */
		max-height: var(--chat-attention-available-height, none);
		overflow-y: auto;
		overscroll-behavior: contain;
	}
	.attention-dock.expanded { overflow: visible; }
	.dock-row { display: flex; align-items: center; gap: var(--space-2); }
	.dock-bar {
		display: flex; align-items: center; gap: var(--space-3); width: 100%;
		min-height: 44px; padding: var(--space-2) var(--space-3);
		border: 1px solid var(--violet-line); border-radius: var(--radius-sm);
		/* The band floats over the live transcript, so a 10%-alpha tint alone let
		   message text read straight through the bar. Keep the violet identity as
		   a tint layered on the canonical glass material its rail peers already
		   use, so the copy sits on an opaque-enough backing. */
		background: linear-gradient(var(--violet-bg), var(--violet-bg)), var(--glass-regular-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		color: var(--text-primary);
		font: inherit; text-align: left; cursor: pointer;
	}
	.dock-row .dock-bar { flex: 1; min-width: 0; width: auto; }
	.dock-mark { width: 8px; height: 8px; flex: none; border-radius: var(--radius-circle); background: var(--violet); }
	.dock-text { display: flex; min-width: 0; flex-direction: column; }
	.dock-text b { overflow: hidden; font-size: var(--text-footnote); text-overflow: ellipsis; white-space: nowrap; }
	.dock-text small { overflow: hidden; color: var(--text-tertiary); font-size: var(--text-caption); text-overflow: ellipsis; white-space: nowrap; }
	.dock-count { margin-left: auto; flex: none; color: var(--text-tertiary); font-size: var(--text-caption); font-variant-numeric: tabular-nums; }
	.dock-action { flex: none; margin-left: auto; color: var(--violet-text); font-size: var(--text-footnote); font-weight: var(--fw-medium); }
	.dock-count + .dock-action { margin-left: 0; }
	.dock-bar:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
	.dock-bar[aria-disabled='true'] { cursor: default; opacity: 0.75; }
	/* These notes float over the live transcript exactly as .dock-bar does, and
	   .dock-deferred renders in the host-access-awaiting state this band widens.
	   Tertiary caption copy on no background reads straight through to the
	   messages underneath, so they get the same backing material as the bar. */
	.dock-receipt,
	.dock-stale,
	.dock-deferred {
		margin: var(--space-1) 0 0;
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-sm);
		background: var(--glass-regular-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		color: var(--text-tertiary);
		font-size: var(--text-caption);
	}
	/* Outside the dock's own padding, so it lines up with the bar below. */
	.dock-receipt { margin-inline: var(--space-4); }
	.sr-only {
		position: absolute; width: 1px; height: 1px; padding: 0;
		margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap;
	}
	@media (max-width: 620px) {
		.attention-dock { padding-left: var(--space-3); padding-right: var(--space-3); }
		/* The count carries the position, so the bar's action label yields room
		   to the question; the whole bar is still the Answer button. */
		.dock-row .dock-action { display: none; }
	}
</style>
