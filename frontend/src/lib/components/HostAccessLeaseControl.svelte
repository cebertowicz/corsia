<script module lang="ts">
	import { get as getStoreValue, writable } from 'svelte/store';

	const inFlightDecisionAttempts = writable(new Map<string, symbol>());

	/*
	 * "A lease is asking the user right now" is deliberately NOT a module
	 * global. A module value outlives every mount by construction, so an
	 * unmounted control — ended session, cleared thread, sign-out — would leave
	 * its last value suppressing a live consumer with no way to clear it. It is
	 * bound out per instance instead, and cleared on teardown.
	 */

	function claimDecision(leaseId: string): symbol | null {
		const attempt = Symbol(leaseId);
		let claimed = false;
		inFlightDecisionAttempts.update((current) => {
			if (current.has(leaseId)) return current;
			const next = new Map(current);
			next.set(leaseId, attempt);
			claimed = true;
			return next;
		});
		return claimed ? attempt : null;
	}

	function isCurrentDecision(leaseId: string, attempt: symbol): boolean {
		return getStoreValue(inFlightDecisionAttempts).get(leaseId) === attempt;
	}

	function releaseDecision(leaseId: string, attempt: symbol): void {
		inFlightDecisionAttempts.update((current) => {
			if (current.get(leaseId) !== attempt) return current;
			const next = new Map(current);
			next.delete(leaseId);
			return next;
		});
	}
</script>

<script lang="ts">
	import { get } from 'svelte/store';
	import { onMount } from 'svelte';
	import { Button, StatePill } from '$lib/components/ui';
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import HostAccessPendingRequestDrawer from './HostAccessPendingRequestDrawer.svelte';
	import HostAccessActiveLeaseChip from './HostAccessActiveLeaseChip.svelte';
	import { decisionBlocked, duration, owner, text } from '$lib/hostAccess/leaseFormat';
	import {
		focusRestoreIntentForDecision,
		restoreComposerFocusAfterLeaseRemoval,
		type FocusRestoreIntent,
	} from '$lib/hostAccess/focusRestore';
	import {
		decideHostAccessLease,
		fetchHostAccessLeases,
		HostAccessApiError,
		type HostAccessLease,
		type HostAccessLeaseDecision,
	} from '$lib/api/hostAccess';
	import { token } from '$lib/stores/auth';
	import { on } from '$lib/ws/events';

	const EXPIRY_REFRESH_BACKOFF_MS = [2_000, 4_000, 30_000] as const;

	let {
		threadId,
		/** Laid out by a slot owner (`ChatAttentionStack`) rather than floating
		 *  itself. Every other layout fact stays this control's own. */
		stacked = false,
		/** Whether a lease is asking the user right now. Bound out so the slot
		 *  owner has ONE precedence rule, and cleared on teardown so the flag
		 *  can never outlive this control. */
		awaitingUser = $bindable(false),
	}: {
		threadId: string | null;
		stacked?: boolean;
		awaitingUser?: boolean;
	} = $props();

	let leases = $state<HostAccessLease[]>([]);
	let stale = $state(false);
	let error = $state('');
	let inFlightDecisionIds = $state<Set<string>>(new Set());
	let nowMs = $state(Date.now());
	let generation = 0;
	let activeRequest: AbortController | null = null;
	const expiryRefreshes = new Map<string, { attempts: number; nextAttemptAt: number }>();
	let expiryRefreshAttempt: symbol | null = null;
	let focusRestoreIntent: FocusRestoreIntent | null = null;

	/** Which single active lease's detail drawer is open, if any. */
	let openActiveLeaseId = $state<string | null>(null);
	let openActiveTrigger: HTMLElement | null = null;
	/** Whether the pending request's drawer is open. Never auto-opens on
	 *  arrival — only a user tap on the trigger sets this. */
	let pendingOpen = $state(false);
	let pendingOpenFor = $state<string | null>(null);
	let pendingTriggerEl = $state<HTMLButtonElement | null>(null);

	const currentPending = $derived(
		leases.find((lease) => lease.state === 'awaiting_user') ?? null,
	);
	$effect(() => {
		awaitingUser = !!currentPending;
		// Teardown runs on unmount, so an ended session, a cleared thread, or a
		// sign-out can never leave a consumer suppressed by a control that is no
		// longer on screen.
		return () => { awaitingUser = false; };
	});
	// The drawer never springs open on its own: it only opens from a tap, and
	// only stays open across a refresh while it is still the same request.
	$effect(() => {
		const id = currentPending?.id ?? null;
		if (id === pendingOpenFor) return;
		pendingOpenFor = id;
		pendingOpen = false;
	});
	const active = $derived(leases.filter((lease) => lease.state === 'active'));
	const transitional = $derived(
		leases.filter((lease) => lease.state === 'reviewing' || lease.state === 'activating'),
	);
	/**
	 * Every shape that is not the compact chip is named once here and rendered
	 * by exactly that name below, so `wide` — their union, and the class
	 * `ChatAttentionStack` styles as a full-band occupant — cannot drift from
	 * what the template puts on screen. Prose in the rail's one-third-width
	 * track is unreadable: at 393px it wraps into a ~94px column.
	 */
	const staleNotice = $derived(
		stale ? error || 'Couldn’t check host access. Try again.' : null,
	);
	const transitionalStatus = $derived(stale ? null : (transitional[0] ?? null));
	const wide = $derived(!!staleNotice || !!transitionalStatus || !!currentPending);
	const decidingLeaseId = $derived(
		leases.find((lease) => inFlightDecisionIds.has(lease.id))?.id ?? null,
	);

	function enclosingLeaseControl(event: MouseEvent): Element | null {
		return event.currentTarget instanceof Element
			? event.currentTarget.closest('[data-adaptive-drawer], .active-chip') // the drawer is portaled out of the chip
			: null;
	}

	function openActiveDrawer(lease: HostAccessLease, event: MouseEvent): void {
		openActiveTrigger = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
		openActiveLeaseId = lease.id;
	}

	function closeActiveDrawer(): void {
		openActiveLeaseId = null;
		openActiveTrigger?.focus();
		openActiveTrigger = null;
	}

	function openPendingDrawer(): void {
		pendingOpen = true;
	}

	function closePendingDrawer(): void {
		pendingOpen = false;
		pendingTriggerEl?.focus();
	}

	async function reconcileAuthoritativeLeases(
		nextLeases: HostAccessLease[],
		requestedThreadId: string,
	): Promise<void> {
		const intent = focusRestoreIntent;
		const leaseWasRemoved = !!intent
			&& intent.threadId === requestedThreadId
			&& !nextLeases.some((lease) => lease.id === intent.leaseId);
		leases = nextLeases;
		if (!leaseWasRemoved || !intent) return;
		focusRestoreIntent = null;
		await restoreComposerFocusAfterLeaseRemoval(
			intent,
			() => threadId === requestedThreadId,
		);
	}

	async function refresh(requestedThreadId = threadId): Promise<void> {
		if (!text(requestedThreadId)) {
			generation += 1;
			activeRequest?.abort();
			activeRequest = null;
			focusRestoreIntent = null;
			leases = [];
			stale = false;
			error = '';
			return;
		}
		const currentToken = get(token);
		if (!currentToken) {
			generation += 1;
			activeRequest?.abort();
			activeRequest = null;
			focusRestoreIntent = null;
			leases = [];
			stale = false;
			error = '';
			return;
		}
		const requestGeneration = ++generation;
		activeRequest?.abort();
		const controller = new AbortController();
		activeRequest = controller;
		try {
			const response = await fetchHostAccessLeases(
				currentToken,
				controller.signal,
				{
					rootThreadId: requestedThreadId,
					nonterminalOnly: true,
				},
			);
			if (requestGeneration !== generation || threadId !== requestedThreadId) return;
			const nextLeases = Array.isArray(response.leases) ? response.leases : [];
			await reconcileAuthoritativeLeases(nextLeases, requestedThreadId);
			stale = false;
			error = '';
		} catch (caught) {
			if (requestGeneration !== generation || threadId !== requestedThreadId) return;
			if (caught instanceof DOMException && caught.name === 'AbortError') return;
			if (leases.length === 0) {
				stale = false;
				error = '';
				return;
			}
			stale = true;
			error = caught instanceof Error
				? caught.message
				: 'Host-access state could not be refreshed.';
		} finally {
			if (requestGeneration === generation && threadId === requestedThreadId) {
				if (activeRequest === controller) activeRequest = null;
			}
		}
	}

	async function decide(
		lease: HostAccessLease,
		decision: HostAccessLeaseDecision,
		decisionControl: Element | null = null,
	): Promise<void> {
		const currentToken = get(token);
		if (!currentToken || decisionBlocked(lease, stale, decidingLeaseId)) return;
		const decisionThreadId = lease.root_thread_id;
		const decisionAttempt = claimDecision(lease.id);
		if (!decisionAttempt) return;
		focusRestoreIntent = focusRestoreIntentForDecision(
			decision, lease.id, decisionThreadId, decisionControl,
		);
		error = '';
		let refreshTruth = true;
		try {
			await decideHostAccessLease(currentToken, lease, decision);
		} catch (caught) {
			if (!isCurrentDecision(lease.id, decisionAttempt) || threadId !== decisionThreadId) return;
			error = caught instanceof Error ? caught.message : 'Host-access decision failed.';
			if (
				caught instanceof HostAccessApiError
				&& (caught.status === 404 || caught.status === 409)
			) {
				// The mutation lost its CAS race; replace local state from HTTP.
			} else {
				stale = true;
				refreshTruth = false;
				if (focusRestoreIntent?.leaseId === lease.id) focusRestoreIntent = null;
			}
		} finally {
			if (
				refreshTruth
				&& isCurrentDecision(lease.id, decisionAttempt)
				&& threadId === decisionThreadId
			) {
				await refresh(decisionThreadId);
			}
			releaseDecision(lease.id, decisionAttempt);
		}
	}

	$effect(() => {
		const selectedThreadId = threadId;
		leases = [];
		stale = false;
		error = '';
		focusRestoreIntent = null;
		openActiveLeaseId = null;
		pendingOpen = false;
		pendingOpenFor = null;
		expiryRefreshes.clear();
		expiryRefreshAttempt = null;
		void refresh(selectedThreadId);
	});

	onMount(() => {
		const stopFocusTracking = (event: FocusEvent) => {
			const intent = focusRestoreIntent;
			if (!intent || !(event.target instanceof HTMLElement)) return;
			if (!intent.control.contains(event.target)) focusRestoreIntent = null;
		};
		document.addEventListener('focusin', stopFocusTracking);
		const stopDecisionTracking = inFlightDecisionAttempts.subscribe((attempts) => {
			inFlightDecisionIds = new Set(attempts.keys());
		});
		const stopChanged = on('host_access_changed', () => { void refresh(); });
		const stopConnected = on('connected', () => { void refresh(); });
		const clock = window.setInterval(() => {
			nowMs = Date.now();
			const activeExpiryKeys = new Set<string>();
			const dueRefreshes: Array<{ attempts: number; nextAttemptAt: number }> = [];
			for (const lease of active) {
				if (!lease.expires_at || Date.parse(lease.expires_at) > nowMs) continue;
				const key = `${lease.id}:${lease.revision}`;
				activeExpiryKeys.add(key);
				let state = expiryRefreshes.get(key);
				if (!state) {
					state = { attempts: 0, nextAttemptAt: nowMs };
					expiryRefreshes.set(key, state);
				}
				if (
					!expiryRefreshAttempt
					&& nowMs >= state.nextAttemptAt
				) dueRefreshes.push(state);
			}
			for (const key of expiryRefreshes.keys()) {
				if (!activeExpiryKeys.has(key)) expiryRefreshes.delete(key);
			}
			if (!dueRefreshes.length || expiryRefreshAttempt) return;
			for (const state of dueRefreshes) {
				const retryDelay = EXPIRY_REFRESH_BACKOFF_MS[
					Math.min(state.attempts, EXPIRY_REFRESH_BACKOFF_MS.length - 1)
				];
				state.attempts += 1;
				state.nextAttemptAt = nowMs + retryDelay;
			}
			const attempt = Symbol('expiry-refresh');
			expiryRefreshAttempt = attempt;
			void refresh().finally(() => {
				if (expiryRefreshAttempt === attempt) expiryRefreshAttempt = null;
			});
		}, 1000);
		return () => {
			generation += 1;
			activeRequest?.abort();
			activeRequest = null;
			focusRestoreIntent = null;
			expiryRefreshAttempt = null;
			document.removeEventListener('focusin', stopFocusTracking);
			stopDecisionTracking();
			stopChanged();
			stopConnected();
			window.clearInterval(clock);
		};
	});
</script>

{#if staleNotice || transitionalStatus || active.length || currentPending}
	<div class="thread-host-access" class:stacked class:wide>
		{#if staleNotice || transitionalStatus || active.length}
			<section class="host-access-status" aria-label="Host access status">
				{#if staleNotice}
					<div class="status-row stale" role="status">
						<StatePill state="error" label="Not current" size="sm" />
						<span>{staleNotice}</span>
						<Button size="sm" onclick={() => { void refresh(); }}>Retry</Button>
					</div>
				{:else if transitionalStatus}
					<div class="status-row" role="status">
						<StatePill state="loading" label={transitionalStatus.state === 'reviewing' ? 'Reviewing' : 'Activating'} size="sm" />
						<span>{owner(transitionalStatus)}: {transitionalStatus.purpose}</span>
					</div>
				{/if}

				{#if active.length}
					<div class="lease-track" aria-label="Active host access">
						{#each active as lease (lease.id)}
							<HostAccessActiveLeaseChip
								{lease}
								{nowMs}
								open={openActiveLeaseId === lease.id}
								deciding={decidingLeaseId === lease.id}
								disabled={decisionBlocked(lease, stale, decidingLeaseId)}
								onOpen={(event) => { openActiveDrawer(lease, event); }}
								onClose={closeActiveDrawer}
								onRevoke={(event) => {
									void decide(lease, 'revoke', enclosingLeaseControl(event));
								}}
							/>
						{/each}
					</div>
				{/if}
			</section>
		{/if}

		{#if currentPending}
			<button
				type="button"
				class="pending-request-trigger"
				aria-label="Review temporary host access"
				aria-haspopup="dialog"
				aria-expanded={pendingOpen}
				bind:this={pendingTriggerEl}
				onclick={openPendingDrawer}
			>
				<span class="eyebrow">Temporary host access</span>
				<span class="pending-owner">{owner(currentPending)}</span>
				<StatePill state={stale ? 'error' : 'warning'} label={stale ? 'Not current' : duration(currentPending.requested_duration_seconds)} size="sm" />
				<span class="pending-action">Review →</span>
			</button>
			{#key currentPending.id}
				<AdaptiveDrawer
					ariaLabel="Review temporary host access"
					open={pendingOpen}
					onClose={closePendingDrawer}
				>
					<HostAccessPendingRequestDrawer
						lease={currentPending}
						{stale}
						deciding={decidingLeaseId === currentPending.id}
						rejectDisabled={decisionBlocked(currentPending, stale, decidingLeaseId)}
						approveDisabled={stale || decidingLeaseId !== null}
						onApprove={() => { void decide(currentPending, 'approve'); }}
						onReject={(event) => { void decide(
							currentPending, 'reject', event.currentTarget instanceof Element ? event.currentTarget : null,
						); }}
						onRetry={() => { void refresh(); }}
					/>
				</AdaptiveDrawer>
			{/key}
		{/if}
	</div>
{/if}

<style>
	.thread-host-access {
		--host-access-available-height: calc(
			100dvh
			- var(--header-bottom)
			- var(--input-bar-height, 100px)
			- 24px
		);
		position: absolute;
		z-index: 11;
		left: 0;
		right: 0;
		bottom: calc(var(--input-bar-height, 100px) + 8px);
		width: min(760px, calc(100% - 32px));
		max-height: var(--host-access-available-height);
		margin: 0 auto;
		display: grid;
		gap: 6px;
		pointer-events: none;
	}
	/* A slot owner supplies position, stacking, width and centring; this control
	   keeps everything internal to itself, including its own height budget. */
	.thread-host-access.stacked {
		position: static;
		width: auto;
		max-width: none;
		margin: 0;
		pointer-events: none;
	}
	.host-access-status {
		display: grid;
		gap: 6px;
	}
	.status-row {
		display: flex;
		align-items: center;
		gap: 9px;
		min-height: 38px;
		padding: 5px 7px 5px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--glass-regular-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-elevation-2);
		font-size: var(--text-footnote);
		color: var(--text-secondary);
		pointer-events: auto;
	}
	.status-row > span:nth-child(2) { flex: 1; min-width: 0; }
	.lease-track {
		display: flex;
		align-items: flex-end;
		gap: 6px;
		overflow-x: auto;
		padding: 1px;
		scrollbar-width: thin;
		pointer-events: none;
	}
	.pending-request-trigger {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		min-height: 44px;
		padding: var(--space-2) var(--space-3);
		border: 1px solid var(--amber-line);
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--amber) 10%, var(--bg-secondary));
		color: var(--text-primary);
		font: inherit;
		text-align: left;
		cursor: pointer;
		pointer-events: auto;
	}
	.pending-request-trigger:focus-visible {
		outline: 2px solid var(--accent-line);
		outline-offset: 2px;
	}
	.pending-owner {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pending-request-trigger .eyebrow {
		flex: none;
		color: var(--amber);
		text-transform: uppercase;
		letter-spacing: 0.07em;
		font-size: var(--text-caption);
		font-weight: 650;
	}
	.pending-action {
		flex: none;
		color: var(--amber);
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
	}

	@media (max-width: 720px) {
		.thread-host-access {
			--host-access-available-height: calc(
				100dvh
				- var(--header-bottom)
				- var(--input-bar-height, 100px)
				- var(--kb-toolbar-inset, 0px)
				- 16px
			);
			width: calc(100% - 16px);
			bottom: calc(
				var(--input-bar-height, 100px)
				+ var(--kb-toolbar-inset, 0px)
				+ 6px
			);
			max-height: var(--host-access-available-height);
		}
		.pending-owner { white-space: normal; }
		.eyebrow { display: none; }
	}
</style>
