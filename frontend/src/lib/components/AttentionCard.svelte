<!--
  AttentionCard.svelte — requires-user artifact card (coordination MVP S6b).

  Solid surface (never glass — required-action content, UX15).
  Two modes:
    user   — clearing controls (S2): answer / acknowledge / dismiss / defer / supersede
    parent — disposition controls (S4): approve / revise / answer-internally / reject + overflow

  Action controls are in AttentionCardActions.svelte to keep both files under 400 LOC.
-->
<script lang="ts">
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { Message, StatePill } from '$lib/components/ui';
	import { fetchReview } from '$lib/api/attention';
	import type { AttentionItem, ReviewResult } from '$lib/api/attention';
	import AttentionCardActions from './AttentionCardActions.svelte';

	interface Props {
		item: AttentionItem;
		/** Controls which action set is rendered. */
		mode?: 'user' | 'parent' | 'readonly';
		/** Called after a successful clear/dispose so the parent can refresh. */
		onDone?: (artifactId: string, outcome: string) => void;
		/** Demo-only: pre-populate review result (bypasses API fetch for /_design). */
		demoReview?: ReviewResult | null;
	}

	const { item, mode = 'user', onDone, demoReview = null }: Props = $props();

	// errorMsg is written by AttentionCardActions via $bindable and shown here
	let errorMsg = $state<string | null>(null);
	let review = $state<ReviewResult | null>(null);
	let reviewLoading = $state(false);
	let reviewError = $state<string | null>(null);
	let hasBlockingReview = $state(false);
	// Non-reactive guard — prevents $effect re-triggering when review state is written.
	let _reviewRequested = false;

	const isDeferred = $derived(item.attention_state === 'deferred');
	const isCleared = $derived(item.attention_state === 'cleared');

	// $derived.by for multi-branch helpers (avoids $derived wrapping a function literal)
	const urgencyState = $derived.by((): 'error' | 'warning' | 'idle' => {
		if (item.urgency === 'high') return 'error';
		if (item.urgency === 'medium') return 'warning';
		return 'idle';
	});
	const urgencyLabel = $derived.by(() => {
		if (item.urgency === 'high') return 'High urgency';
		if (item.urgency === 'medium') return 'Medium urgency';
		return 'Low urgency';
	});
	const typeLabel = $derived(item.subtype ? `${item.type} · ${item.subtype}` : item.type ?? '');
	const stateLabel = $derived(isDeferred ? 'Deferred' : isCleared ? 'Cleared' : 'Needs action');
	const stateVariant = $derived(
		isDeferred ? ('warning' as const) : isCleared ? ('success' as const) : ('idle' as const)
	);

	// owner_thread from the backend is a raw thread ID. Only display when it looks
	// like a human-readable title (contains whitespace or the middle-dot separator '·').
	const ownerDisplay = $derived.by(() => {
		const o = item.owner_thread;
		if (!o || !/[\s·]/.test(o)) return null;
		return o;
	});

	// ── Review profile ─────────────────────────────────────────────────────────
	async function loadReview() {
		if (_reviewRequested) return;
		_reviewRequested = true;
		reviewLoading = true;
		reviewError = null;
		try {
			const tok = get(token);
			if (!tok) throw new Error('Not authenticated');
			review = await fetchReview(tok, item.external_id);
			// Use blocking_failure OR may_approve_to_user=false as the gate signal.
			hasBlockingReview = review.blocking_failure || !review.may_approve_to_user;
		} catch (e: unknown) {
			const status = (e as { status?: number }).status;
			if (status === 404) {
				// INTENTIONAL FAIL-OPEN: no review profile configured means unconstrained approve.
				// Absence of a profile = "no restrictions set," not "missing gate."
				// Backend disposition endpoint enforces outcome permissions independently.
				review = { artifact_id: item.external_id, blocking_failure: false, may_approve_to_user: true, findings: [] };
				hasBlockingReview = false;
			} else {
				// Genuine fetch failure — surface a product-voice notice; never the raw error.
				reviewError = 'Review profile unavailable — approve with caution';
			}
		} finally {
			reviewLoading = false;
		}
	}

	// Load review when in parent mode so blocking state is visible before approve.
	// demoReview bypasses the API (for /_design preview only).
	$effect(() => {
		if (mode === 'parent' && !isCleared) {
			if (demoReview !== null) {
				review = demoReview;
				hasBlockingReview = demoReview.blocking_failure || !demoReview.may_approve_to_user;
			} else {
				loadReview();
			}
		}
	});
</script>

<!-- ── Card ──────────────────────────────────────────────────────────────── -->
<article
	class="attention-card"
	class:cleared={isCleared}
	class:deferred={isDeferred}
	aria-label="Requires your attention"
>
	<!-- Header: artifact name (if present) + type label + urgency + state pills -->
	<header class="card-header">
		<div class="type-info">
			<span
				class="indicator"
				class:high={item.urgency === 'high'}
				class:medium={item.urgency === 'medium'}
			></span>
			{#if item.name}
				<span class="item-name" title={item.name}>{item.name}</span>
			{/if}
			{#if typeLabel}
				<span class="type-label">{typeLabel}</span>
			{/if}
		</div>
		<div class="header-pills">
			{#if item.urgency && item.urgency !== 'low'}
				<StatePill state={urgencyState} label={urgencyLabel} size="sm" />
			{/if}
			<StatePill state={stateVariant} label={stateLabel} size="sm" />
		</div>
	</header>

	<!-- Body: requested action + owner context + defer condition -->
	<div class="card-body">
		{#if item.requested_action}
			<p class="requested-action">{item.requested_action}</p>
		{/if}
		{#if ownerDisplay}
			<p class="card-meta">From thread: <span class="meta-value">{ownerDisplay}</span></p>
		{/if}
		{#if isDeferred && item.defer_condition}
			<p class="card-meta">
				Deferred until:
				<span class="meta-value">{item.defer_condition.value ?? item.defer_condition.type}</span>
			</p>
		{/if}
	</div>

	<!-- Review profile (parent mode only) — shown BEFORE the Approve button -->
	{#if mode === 'parent' && !isCleared}
		{#if reviewLoading}
			<p class="review-loading">Checking review profile…</p>
		{:else if reviewError}
			<Message variant="info">{reviewError}</Message>
		{:else if review && review.blocking_failure}
			<!-- blocking_failure=true: approval is gated; show error before Approve -->
			<Message variant="err">
				Review profile flagged a blocking issue — approval is blocked until resolved or an
				authorized override is supplied.
				{#if review.findings?.length}
					<ul class="review-findings">
						{#each review.findings as f}
							<li class="finding-{f.severity}">{f.message}</li>
						{/each}
					</ul>
				{/if}
			</Message>
		{/if}
	{/if}

	<!-- Actionable error from clearing/disposition calls -->
	{#if errorMsg}
		<Message variant="err">{errorMsg}</Message>
	{/if}

	<!-- All clearing/disposition controls (owned by child to stay under 400 LOC) -->
	<AttentionCardActions
		{item}
		{mode}
		{isDeferred}
		{isCleared}
		{hasBlockingReview}
		{onDone}
		bind:errorMsg
	/>
</article>

<style>
	.attention-card {
		background: var(--bg-secondary);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 12px 14px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		position: relative;
		overflow: hidden;
	}
	/* 3px left indicator — amber for raised, muted for deferred, green for cleared */
	.attention-card::before {
		content: '';
		position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
		background: var(--amber); border-radius: 0 2px 2px 0; opacity: 0.7;
	}
	.attention-card.deferred::before { background: var(--text-tertiary); }
	.attention-card.cleared::before { background: var(--green); opacity: 0.5; }

	/* Header */
	.card-header {
		display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0;
	}
	.type-info { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; }
	.indicator {
		flex-shrink: 0; width: 7px; height: 7px;
		border-radius: var(--radius-circle); background: var(--text-tertiary);
	}
	.indicator.high { background: var(--red); }
	.indicator.medium { background: var(--amber); }
	.item-name {
		font-size: var(--text-subhead); font-weight: 600; color: var(--text-primary);
		white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
	}
	.type-label {
		font-size: var(--text-footnote); font-weight: 600; color: var(--text-secondary);
		text-transform: uppercase; letter-spacing: 0.06em;
		white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
	}
	.header-pills { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }

	/* Body */
	.card-body { display: flex; flex-direction: column; gap: 4px; }
	.requested-action {
		font-size: var(--text-callout); color: var(--text-primary);
		line-height: var(--lh-normal, 1.45); margin: 0;
		overflow-wrap: break-word; word-break: break-word;
	}
	.requested-action.muted { color: var(--text-tertiary); font-style: italic; }
	.card-meta {
		font-size: var(--text-footnote); color: var(--text-secondary);
		margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.meta-value { color: var(--text-primary); font-weight: 500; }

	/* Review profile warning */
	.review-loading {
		font-size: var(--text-footnote); color: var(--text-tertiary); margin: 0;
	}
	.review-findings { margin: 4px 0 0; padding-left: 16px; font-size: var(--text-footnote); }
	.review-findings li { margin: 2px 0; }
	.finding-blocking, .finding-high, .finding-error { color: var(--red); }
	.finding-medium, .finding-warning { color: var(--amber); }
</style>
