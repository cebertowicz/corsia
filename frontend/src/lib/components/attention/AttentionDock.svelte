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
	import { onMount, tick, type Snippet } from 'svelte';
	import CommentReplyChipRow from './CommentReplyChipRow.svelte';
	import { replyItemsForThread } from './replyItems';
	import { get } from 'svelte/store';
	import { on } from '$lib/ws';
	import { connectionState } from '$lib/stores/connection';
	import { token } from '$lib/stores/auth';
	import { emitProductEvent } from '$lib/product_events';
	import { forgetAutoOpen, holdsDraft, judgeAutoOpen, noteLoad } from '$lib/stores/attentionAutoOpen';
	import { activeThreadId, messages } from '$lib/stores/threads';
	import DecisionChip from './DecisionChip.svelte';
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import ExternalReviewRow from './ExternalReviewRow.svelte';
	import { dockQueue as getDockQueue, label, decisionMeta, regionLabel as getRegionLabel } from './dockPresentation';
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
	import type { AttentionDestination, DecisionDetail, ExternalReviewDetail } from '$lib/api/attentionQueue';

	let {
		writesDisabled = false,
		suppressed = false,
		onOpenDestination = () => {},
		showing = $bindable(false),
		scrollControl,
	}: {
		writesDisabled?: boolean;
		/** A higher-precedence control holds this slot right now. */
		suppressed?: boolean;
		onOpenDestination?: (destination: AttentionDestination, threadId?: string | null) => void;
		showing?: boolean;
		scrollControl?: Snippet;
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

	const dockQueue = $derived(getDockQueue($visibleQueue));
	const current = $derived(shownItem(dockQueue, $activeId));
	const hasReplies = $derived(replyItemsForThread($visibleQueue, $activeThreadId).length > 0);
	$effect(() => { showing = !!current || hasReplies; });
	const position = $derived(
		current ? dockQueue.items.findIndex((i) => i.external_id === current.external_id) + 1 : 0,
	);
	const canSkip = $derived(dockQueue.items.length > 1);
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

	const regionLabel = $derived(getRegionLabel(current, dockQueue.count));

	async function open() {
		if (!current || suppressed) return;
		expand(current, suppressed);
		// Opening replaces the focused bar. Move keyboard focus into the new card
		// so the next Tab continues from its controls rather than falling to the page.
		await tick();
		region?.querySelector<HTMLElement>('[aria-label="Collapse reply"]')?.focus();
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
		// A decision raised by the viewed thread opens by itself once the person is not typing,
		// nothing holds the slot and they've had time to read what came before it (see the store).
		void busyTick;
		const typingLeft = lastKeydown + TYPING_IDLE_MS - Date.now();
		const outcome = judgeAutoOpen(dockQueue.items, {
			viewedThreadId: $activeThreadId,
			expandedId: $expandedId,
			suppressed,
			busy: typingLeft > 0 || holdsDraft(document.activeElement),
			messages: get(messages), // untracked: a streaming reveal rewrites rows constantly
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
		const holdLeft = outcome.state === 'wait' ? Math.max(typingLeft, outcome.retryInMs ?? 0) : 0;
		if (holdLeft <= 0) return;
		const timer = setTimeout(() => { busyTick += 1; }, holdLeft);
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


</script>

{#if recording}
	<!-- The answer is already out of the queue; this is its receipt until the
	     write confirms, and a failed write returns the decision with its reason. -->
	<p class="dock-receipt" role="status">Answer recorded</p>
{:else if $answerNotice}
	<!-- The question left the queue before this answer reached it. -->
	<p class="dock-receipt" role="status">{$answerNotice}</p>
{/if}
{#if current || hasReplies}
	<div
		class="attention-dock"
		class:expanded={showChip}
		data-attention-dock={current ? '' : undefined}
	>
		<CommentReplyChipRow onOpenDocument={onOpenDestination} />
		{#if current}
		<section bind:this={region} role="region" aria-label={regionLabel}>
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
				{#if dockQueue.count > 1}
					<span class="dock-count">{position} of {dockQueue.count}</span>
				{/if}
				<span class="dock-action">
					{failed ? 'Not saved — Retry' : current.renderer === 'decision' ? 'Answer' : 'Review'}
				</span>
				<svg class="dock-chevron" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
			</button>
			</div>
		{/if}
		{#if current.renderer === 'decision'}
			{#key current.external_id}
				<AdaptiveDrawer
					ariaLabel={`Decision — ${label(current)}`}
					contextLabel={decisionMeta(current.detail as DecisionDetail)}
					opaque
					flushBottom
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
		</section>
		{/if}
		{@render scrollControl?.()}
	</div>
{/if}

<style>
	.attention-dock {
		flex: none;
		/* The slot's shadow gutter (none outside the slot). A scroller clips at its padding
		   edge, so the dock pads into that gutter at the sides and bottom and its cards'
		   rounded corners and drop shadows paint there instead of being cut flat at the
		   card edge. The negative margins keep the layout box on the band's pixels. */
		--dock-gutter: var(--chat-attention-shadow-gutter, 0px);
		/* No side inset of its own: the chat column places it, so its content edges are the transcript's. */
		padding: var(--space-4) var(--dock-gutter) var(--dock-gutter);
		margin: 0 calc(-1 * var(--dock-gutter)) calc(-1 * var(--dock-gutter));
		/* Transcript scrolling beneath fades out across that top inset, so the row's edge
		   reads without a border or a tint. Invisible when nothing is under it. It covers
		   the band only, never the gutter. */
		background: linear-gradient(to bottom, transparent, var(--bg-primary) var(--space-4))
			center top / calc(100% - 2 * var(--dock-gutter)) calc(100% - var(--dock-gutter)) no-repeat;
		/* The chat slot publishes this height budget so an opened row scrolls
		   inside it instead of growing off the top of the viewport (measured at
		   320px: heading and close button at y = -91). The bottom gutter is outside it. */
		max-height: calc(var(--chat-attention-available-height, 100dvh) + var(--dock-gutter));
		overflow-y: auto;
		overscroll-behavior: contain;
	}
	.attention-dock :global(.reply-chip-notice) { margin-bottom: var(--space-2); }
	.dock-row { display: flex; align-items: center; gap: var(--space-2); }
	.dock-bar {
		display: flex; align-items: center; gap: var(--space-3); width: 100%;
		/* Needs-you is a raised card, distinct from the transcript's worked chips. */
		min-height: 44px; padding: var(--space-2) var(--space-2) var(--space-2) 2px;
		/* Needs-you is the composer's frosted card: its glass, hairline, corner and soft shadow. */
		border: 1px solid var(--border); border-radius: var(--radius-xl);
		background: var(--glass-thin); box-shadow: var(--glass-rim), var(--shadow-chip);
		color: var(--text-primary);
		font: inherit; text-align: left; cursor: pointer;
		transition: background var(--dur-tight) var(--ease-snap);
	}
	@media (hover: hover) { .dock-bar:hover { background: var(--bg-hover); } }
	.dock-row .dock-bar { flex: 1; min-width: 0; width: auto; }
	/* "Needs you" is amber in the chat view (sidebar, plan, artifact row, this row). */
	.dock-mark { display: flex; width: 36px; flex: none; align-items: center; justify-content: center; }
	.dock-mark::before { content: ''; width: 8px; height: 8px; border-radius: var(--radius-circle); background: var(--amber); }
	.dock-text { display: flex; min-width: 0; flex: 1 1 auto; flex-direction: column; }
	.dock-text b { overflow: hidden; font-size: var(--text-callout); font-weight: var(--fw-semibold); line-height: var(--lh-tight); text-overflow: ellipsis; white-space: nowrap; }
	.dock-text small { overflow: hidden; color: var(--text-secondary); font-size: var(--text-footnote); line-height: var(--lh-tight); text-overflow: ellipsis; white-space: nowrap; }
	.dock-count { flex: none; color: var(--text-tertiary); font-size: var(--text-caption); font-variant-numeric: tabular-nums; }
	/* A trailing action reads as the plan's do: caption, tertiary, primary on hover. */
	.dock-action { flex: none; color: var(--text-tertiary); font-size: var(--text-caption); }
	@media (hover: hover) { .dock-bar:hover .dock-action { color: var(--text-primary); } }
	.dock-chevron { flex: none; color: var(--text-tertiary); }
	.dock-bar:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
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
	.sr-only {
		position: absolute; width: 1px; height: 1px; padding: 0;
		margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap;
	}
	@media (max-width: 620px) {
		/* The count carries the position, so the bar's action label yields room
		   to the question; the whole bar is still the Answer button. */
		.dock-row .dock-action { display: none; }
	}
</style>
