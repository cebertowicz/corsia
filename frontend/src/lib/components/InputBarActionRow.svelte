<script module lang="ts">
	// USER-REPORTS #1: on iOS a deliberate hold sent as `apply_now`, so the hold
	// appeared to steer. The hold armed at 600ms, but Safari claims a touch it
	// reads as a long-press or the start of a pan well before that and fires
	// `pointercancel` — which disarmed the hold. The arm now lands before the
	// platform can claim the touch. Exported so the gesture's tests and the CSS
	// that animates it read the same number this component holds to.
	export const QUEUE_ARM_MS = 400;
</script>

<script lang="ts">
	import { onDestroy } from 'svelte';
	import { connectionState } from '$lib/stores/connection';
	import { haptics } from '$lib/haptic';
	import { stopRequestedTurns } from '$lib/ws/send';
	import { turnStates } from '$lib/ws/turnState';
	import { isThreadTurnActive } from '$lib/stores/threads';
	import type { SheetView } from './inputBarHelpers';
	import { inlineSlot } from '$lib/slotMotion';
	import SwitchingIcon from './ui/SwitchingIcon.svelte';

	let {
		threadId,
		sheetView,
		openSheet,
		isActiveModelReady,
		canSendToModel,
		queueUntilSwitch,
		switchState = null,
		switchError = null,
		modelLabel,
		activeModelName,
		supportsReasoning,
		effortLabel,
		canInspectContext,
		contextThreadName,
		contextUtilization,
		contextPct,
		contextTokenSource,
		contextCountsAgainstBudget,
		inputText,
		pendingAttachmentCount,
		hasMessageToSend,
		onSendClick,
		onQueueAfterTurn,
	}: {
		threadId: string | null;
		sheetView: SheetView;
		openSheet: (view: Exclude<SheetView, null>) => void;
		isActiveModelReady: boolean;
		canSendToModel: boolean;
		queueUntilSwitch: boolean;
		switchState?: 'model' | 'account' | 'failed' | null;
		switchError?: string | null;
		modelLabel: string;
		activeModelName: string | undefined;
		supportsReasoning: boolean;
		effortLabel: string;
		canInspectContext: boolean;
		contextThreadName: string;
		contextUtilization: number | null;
		contextPct: number | null;
		contextTokenSource: 'measured' | 'estimated' | null;
		/** True when the percentage is against CORSIA's checkpoint budget, not the
		 *  model's window, so it can honestly read above 100% (#1753). */
		contextCountsAgainstBudget: boolean;
		inputText: string;
		pendingAttachmentCount: number;
		hasMessageToSend: boolean;
		onSendClick: () => void;
		onQueueAfterTurn: () => void;
	} = $props();

	// Finger drift over a 40px target is not a drag. Past this, it is.
	const HOLD_SLOP_PX = 12;
	// A gesture the platform aborts produces no `click` at all, so a boolean
	// "suppress the next click" outlives its own gesture and eats the user's
	// NEXT tap — a send that silently does nothing. Bound it in time instead.
	const CLICK_SUPPRESSION_MS = 700;

	let queueHoldTimer: ReturnType<typeof setTimeout> | null = null;
	let queueHolding = $state(false);
	let queueArmed = $state(false);
	let holdStartedAt = 0;
	let holdOrigin: { x: number; y: number } | null = null;
	let suppressClickUntil = 0;

	// Per-thread, not global: this row may be bound to a subagent thread while
	// a different thread is the app's own active selection (#1949).
	const isStreamingThisThread = $derived(isThreadTurnActive($turnStates, threadId));
	// Stop is only "pending" while the exact turn it was asked of is still live.
	// The moment that turn ends the control resolves on its own, so a working
	// Stop no longer looks identical to a broken one.
	const stoppingTurn = $derived.by(() => {
		if (!threadId) return false;
		const state = $turnStates.get(threadId);
		if (state?.phase !== 'active' || state.turnId === null) return false;
		return $stopRequestedTurns.get(threadId) === state.turnId;
	});
	const showStop = $derived(
		isStreamingThisThread && !inputText.trim() && pendingAttachmentCount === 0,
	);

	function openControlSheet(event: MouseEvent, view: Exclude<SheetView, null>) {
		event.preventDefault();
		event.stopPropagation();
		openSheet(view);
	}

	function beginQueueHold(event: PointerEvent) {
		if (!hasMessageToSend || !canSendToModel || queueHoldTimer) return;
		queueArmed = false;
		queueHolding = true;
		holdStartedAt = Date.now();
		holdOrigin = { x: event.clientX, y: event.clientY };
		// Explicit capture keeps the gesture bound to this button for its whole
		// life, so travel is judged by distance rather than by boundary events —
		// which is what let a 40px target disarm itself under a steady thumb.
		const target = event.currentTarget as HTMLElement | null;
		if (target && typeof event.pointerId === 'number') {
			try { target.setPointerCapture(event.pointerId); } catch { /* not captureable */ }
		}
		queueHoldTimer = setTimeout(() => {
			queueHoldTimer = null;
			queueArmed = true;
			haptics.threshold();
		}, QUEUE_ARM_MS);
	}

	function cancelQueueHold() {
		if (queueHoldTimer) clearTimeout(queueHoldTimer);
		queueHoldTimer = null;
		queueArmed = false;
		queueHolding = false;
		holdOrigin = null;
	}

	onDestroy(cancelQueueHold);

	/** A hold that travelled is a drag or a scroll, not a deliberate hold. */
	function trackQueueHold(event: PointerEvent) {
		if (!holdOrigin) return;
		const dx = event.clientX - holdOrigin.x;
		const dy = event.clientY - holdOrigin.y;
		if (dx * dx + dy * dy > HOLD_SLOP_PX * HOLD_SLOP_PX) cancelQueueHold();
	}

	function queueAfterTurn() {
		cancelQueueHold();
		suppressClickUntil = Date.now() + CLICK_SUPPRESSION_MS;
		onQueueAfterTurn();
	}

	function completeQueueHold() {
		if (!queueArmed) { cancelQueueHold(); return; }
		queueAfterTurn();
	}

	/**
	 * Finish a hold the platform took away from us.
	 *
	 * Once the arm delay has elapsed the user has already made the gesture, so
	 * iOS deciding the touch belongs to it must not turn their queue into a
	 * steer. Below the delay it is an ordinary abort.
	 */
	function abandonQueueHold() {
		const held = queueArmed
			|| (queueHolding && Date.now() - holdStartedAt >= QUEUE_ARM_MS);
		if (!held) { cancelQueueHold(); return; }
		queueAfterTurn();
	}

	function handleSendClick() {
		if (Date.now() < suppressClickUntil) {
			suppressClickUntil = 0;
			return;
		}
		onSendClick();
	}
</script>

<div class="action-row">
	<button
		type="button"
		class="plus-btn"
		class:open={sheetView === 'attachments'}
		title={sheetView === 'attachments' ? 'Close' : 'Attach'}
		data-sheet-trigger
		onclick={(event) => openControlSheet(event, 'attachments')}
	>
		<svg class="plus-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
	</button>
	<button
		type="button"
		class="row-chip model-chip"
		class:open={sheetView === 'model'}
		class:unloaded={!isActiveModelReady}
		data-sheet-trigger
		onclick={(event) => openControlSheet(event, 'model')}
		title={switchState === 'failed'
			? switchError ?? 'Switch failed'
			: switchState === 'model'
				? 'Model switch in progress'
				: switchState === 'account'
					? 'Account switch in progress'
					: isActiveModelReady
						? activeModelName
						: queueUntilSwitch
							? 'Model switch in progress'
							: 'Pick a loaded model to send'}
	>
		{#if switchState}
			<span class="model-switch-slot" in:inlineSlot out:inlineSlot>
				<span class="model-switch-status" class:failed={switchState === 'failed'}>
					<span class="model-switch-icon" aria-hidden={switchState === 'failed'}><SwitchingIcon ariaLabel={switchState === 'model' ? 'Switching model' : 'Switching account'} /></span>
					<span class="model-status-dot" style="background: var(--red)" role="img" aria-label="Switch failed" aria-hidden={switchState !== 'failed'}></span>
				</span>
			</span>
		{/if}
		<span class="model-chip-name">{modelLabel}</span>
		<svg class="chevron" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"/></svg>
	</button>
	{#if supportsReasoning && effortLabel}
		<button
			type="button"
			class="row-chip effort-chip"
			class:open={sheetView === 'thinking'}
			data-sheet-trigger
			onclick={(event) => openControlSheet(event, 'thinking')}
			title="Reasoning level"
		>
			<span>{effortLabel}</span>
			<svg class="chevron" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"/></svg>
		</button>
	{/if}
	{#if canInspectContext}
		<button
			type="button"
			class="row-chip context-chip"
			class:warn={contextUtilization !== null && contextUtilization >= 0.7 && contextUtilization < 0.9}
			class:crit={contextUtilization !== null && contextUtilization >= 0.9}
			class:open={sheetView === 'context'}
			data-sheet-trigger
			onclick={(event) => openControlSheet(event, 'context')}
			aria-label={contextUtilization === null
				? `Context for ${contextThreadName}`
				: contextTokenSource === 'estimated'
					? `Context for ${contextThreadName}: estimated ${contextPct ?? Math.round(Math.min(1, contextUtilization) * 100)}%`
					: `Context for ${contextThreadName}: ${contextPct ?? Math.round(Math.min(1, contextUtilization) * 100)}% used`}
			title={contextUtilization === null
				? 'Inspect context sources'
				: contextTokenSource === 'estimated'
					? 'Estimated next-turn context'
					: contextCountsAgainstBudget
						? 'Context used, against this thread\u2019s checkpoint budget'
						: 'Context window'}
		>
			{#if contextUtilization !== null}
				<svg class="ctx-ring" width="14" height="14" viewBox="0 0 16 16">
					<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" opacity="0.22" stroke-width="2.4"/>
					<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="2.4"
						stroke-linecap="round"
						stroke-dasharray={2 * Math.PI * 5.5}
						stroke-dashoffset={2 * Math.PI * 5.5 * (1 - Math.min(1, contextUtilization))}
						transform="rotate(-90 8 8)"
					/>
				</svg>
				<span class="ctx-pct"><span class="ctx-estimate-marker" class:estimated={contextTokenSource === 'estimated'} aria-hidden="true">~</span>{contextPct ?? Math.round(Math.min(1, contextUtilization) * 100)}%</span>
			{:else}
				<!-- Unknown is not progress: an idle thread must not look like it is loading. -->
				<svg class="ctx-ring" width="14" height="14" viewBox="0 0 16 16">
					<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" opacity="0.22" stroke-width="2.4"/>
				</svg>
			{/if}
		</button>
	{/if}
	<div class="action-spacer"></div>
	<button
		type="button"
		class="send-btn"
		class:queue-holding={queueHolding}
		class:queue-armed={queueArmed}
		class:stop-btn={showStop}
		class:stopping={showStop && stoppingTurn}
		style="--queue-arm-ms: {QUEUE_ARM_MS}ms"
		onpointerdown={beginQueueHold}
		onpointermove={trackQueueHold}
		onpointerup={completeQueueHold}
		onpointercancel={abandonQueueHold}
		oncontextmenu={(event) => event.preventDefault()}
		onclick={handleSendClick}
		disabled={(!hasMessageToSend && !isStreamingThisThread) || $connectionState !== 'connected' || (!canSendToModel && hasMessageToSend)}
		aria-label={queueArmed
			? 'Release to queue after the current turn'
			: isStreamingThisThread && !hasMessageToSend
				? stoppingTurn ? 'Stopping this turn' : 'Stop turn'
				: 'Send message'}
		title={queueArmed
			? 'Release to queue after the current turn'
			: queueUntilSwitch && !isStreamingThisThread
				? 'Queues until model switch finishes'
				: !canSendToModel && !isStreamingThisThread
					? 'Pick a loaded model to send'
					: hasMessageToSend
						? 'Send — hold to queue, or press Ctrl/⌘+Enter'
						: undefined}
	>
		{#if queueArmed}
			<!-- The gesture has landed and the user has not let go yet. Saying so
			     only after release is why a hold that queued and a hold that
			     steered looked the same until the message was already gone. -->
			<svg class="send-glyph" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M12 4v10M8 10l4 4 4-4M4 20h16"/>
			</svg>
		{:else if showStop}
			<svg class="send-glyph" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>
		{:else}
			<svg class="send-glyph" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M12 19V5M5 12l7-7 7 7"/>
			</svg>
		{/if}
	</button>
</div>

<style>
	.model-chip-name { min-width: 0; }
	.model-switch-slot { display: inline-flex; flex: 0 0 auto; align-items: center; }
	/* Icon and failed dot share one cell, so the slot keeps the icon's size as one fades into the other. */
	.model-switch-status { display: grid; place-items: center; }
	.model-switch-status > * { grid-area: 1 / 1; transition: opacity var(--dur-tight) var(--ease-snap); }
	.model-switch-icon { display: inline-flex; }
	.model-switch-status.failed .model-switch-icon, .model-switch-status:not(.failed) .model-status-dot { opacity: 0; pointer-events: none; }
	.ctx-estimate-marker {
		visibility: hidden;
		font: inherit;
		font-size: 0.75em;
		pointer-events: none;
	}
	.ctx-estimate-marker.estimated { visibility: visible; }
</style>
