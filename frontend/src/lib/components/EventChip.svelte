<script lang="ts">
	import { requestDeliveryAction } from '$lib/deliveryActions';
	import { isLongMessageText } from '$lib/messageCollapse';
	import { deliveryPresentation } from '$lib/messageDelivery';
	import { limitHeldThreadId } from '$lib/stores/overageSetting';
	import { token } from '$lib/stores/auth';
	import { activeThreadId, isThreadTurnActive } from '$lib/stores/threads';
	import { turnStates } from '$lib/ws/turnState';
	import { parseWsTimestamp } from '$lib/time';
	import { passiveContextEventLabel } from './messageListModel';
	import { needsWakeRetry, wakeRetryControl } from './pendingWakeRetry';

	let { msg, deliveryOnly = false }: { msg: any; deliveryOnly?: boolean } = $props();

	let summaryExpanded = $state(false);
	let detailsExpanded = $state(false);
	let renderedMsgId = $state<string | null>(null);
	let deliveryActionPending = $state(false);

	$effect(() => {
		if (msg.id !== renderedMsgId) {
			summaryExpanded = false;
			detailsExpanded = false;
			renderedMsgId = msg.id;
		}
	});

	const eventType = $derived((msg.metadata?.event_type as string) ?? '');
	const eventDetails = $derived(
		Array.isArray(msg.metadata?.context_events) ? msg.metadata.context_events : [],
	);
	const digestState = $derived(
		eventType === 'context.digest'
			? (msg.delivery_state === 'queued' ? 'pending' : 'delivered')
			: undefined,
	);
	const summary = $derived(
		msg.metadata?.type === 'context_event'
			? passiveContextEventLabel(msg)
			: (msg.metadata?.summary as string) || msg.content || '',
	);
	const summaryIsLong = $derived(isLongMessageText(summary));
	const summaryCollapsed = $derived(summaryIsLong && !summaryExpanded);

	const rawUrl = $derived((msg.metadata?.url as string) || null);
	const url = $derived(rawUrl && /^https?:\/\//i.test(rawUrl) ? rawUrl : null);
	const messageThreadId = $derived(
		typeof msg.thread_id === 'string' && msg.thread_id ? msg.thread_id : $activeThreadId,
	);
	const isWakeEvent = $derived(msg.metadata?.event_disposition === 'wake');
	const messageTurnActive = $derived(isThreadTurnActive($turnStates, messageThreadId));
	const delivery = $derived(deliveryPresentation(msg, messageTurnActive, messageThreadId !== null && messageThreadId === $limitHeldThreadId));
	const canManageDelivery = $derived(isWakeEvent && !!messageThreadId && !!$token);
	const canCancel = $derived(
		canManageDelivery
		&& msg.delivery_state === 'queued'
	);
	const canRetry = $derived(canManageDelivery && needsWakeRetry(msg));
	const retryControl = $derived(wakeRetryControl(msg));

	function label(type: string): string {
		if (type === 'person_step.done') return 'Done';
		if (type === 'person_step.wont_do') return 'Won’t do';
		if (type === 'model.switched') return 'Model';
		if (type.startsWith('plan.')) return 'Plan';
		if (type.startsWith('checkpoint.')) return 'Checkpoint';
		if (type === 'context.digest') return 'Context';
		if (type.startsWith('system.restart.')) return 'Restart';
		if (type.includes('review')) return 'Review';
		if (type.includes('comment')) return 'PR comment';
		if (type.includes('check')) return 'Check';
		if (type.includes('pr_') || type.includes('pull_request')) return 'PR';
		const last = type.split('.').pop() || 'Event';
		return last.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
	}

	function detailSummary(detail: any): string {
		return String(detail?.summary || detail?.content || 'Context update');
	}

	function detailTime(detail: any): string {
		const at = parseWsTimestamp(detail?.created_at);
		return Number.isNaN(at)
			? ''
			: new Date(at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
	}

	async function runWakeDeliveryAction(action: 'cancel' | 'retry'): Promise<void> {
		const threadId = messageThreadId;
		const authToken = $token;
		const allowed = action === 'retry' ? canRetry : canCancel;
		if (!allowed || !threadId || !authToken || deliveryActionPending) return;
		deliveryActionPending = true;
		try {
			await requestDeliveryAction(action, {
				threadId,
				messageId: msg.id,
				authToken,
				failureText: action === 'retry'
					? 'Could not retry this wake'
					: 'Could not cancel this wake',
			});
		} finally {
			deliveryActionPending = false;
		}
	}
</script>

<div class="ev-wrap">
	{#if !deliveryOnly}
	{#if url}
		<a class="ev-chip" data-context-digest={digestState} href={url} target="_blank" rel="noopener noreferrer" aria-label={`Open event on GitHub: ${summary}`}>
			{#if digestState === 'pending'}
				<svg class="ev-state" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
			{:else if digestState === 'delivered'}
				<svg class="ev-state delivered" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>
			{/if}
			<span class="ev-label">{label(eventType)}</span>
			<span class="ev-summary long-message-body" class:collapsed={summaryCollapsed} class:expanded={summaryIsLong && summaryExpanded}>{summary}</span>
			<span class="ev-link" aria-hidden="true">↗</span>
		</a>
	{:else}
		<div class="ev-chip" data-context-digest={digestState}>
			{#if digestState === 'pending'}
				<svg class="ev-state" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
			{:else if digestState === 'delivered'}
				<svg class="ev-state delivered" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>
			{/if}
			<span class="ev-label">{label(eventType)}</span>
			<span class="ev-summary long-message-body" class:collapsed={summaryCollapsed} class:expanded={summaryIsLong && summaryExpanded}>{summary}</span>
		</div>
	{/if}
	{#if eventDetails.length > 0}
		<button
			type="button"
			class="ev-details-toggle"
			data-event-disclosure
			aria-expanded={detailsExpanded}
			onclick={() => (detailsExpanded = !detailsExpanded)}
		>
			<span>{detailsExpanded ? 'Hide details' : 'Show details'}</span>
			<svg class:expanded={detailsExpanded} viewBox="0 0 24 24" aria-hidden="true"><path d="m8 10 4 4 4-4"/></svg>
		</button>
		{#if detailsExpanded}
			<ul class="ev-details">
				{#each eventDetails as detail (detail.id)}
					<li class="ev-detail">
						<span>{detailSummary(detail)}</span>
						{#if detailTime(detail)}<time>{detailTime(detail)}</time>{/if}
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
	{#if summaryIsLong}
		<button
			class="long-message-toggle ev-more"
			aria-expanded={summaryExpanded}
			onclick={() => (summaryExpanded = !summaryExpanded)}
		>
			{summaryExpanded ? 'Show less' : 'Show more'}
		</button>
	{/if}
	{/if}
	{#if (isWakeEvent || deliveryOnly) && delivery}
		<div class="ev-delivery">
			<span class="ev-delivery-status" title={delivery.title}>{delivery.label}</span>
			{#if canRetry}
				<button
					type="button"
					class="ev-delivery-action"
					data-delivery-action={retryControl.action}
					disabled={deliveryActionPending}
					title={retryControl.title}
					onclick={() => { void runWakeDeliveryAction('retry'); }}
				>{retryControl.label}</button>
			{/if}
			{#if canCancel}
				<button
					type="button"
					class="ev-delivery-action"
					data-delivery-action="cancel"
					disabled={deliveryActionPending}
					title="Cancel this queued system wake"
					onclick={() => { void runWakeDeliveryAction('cancel'); }}
				>Cancel</button>
			{/if}
		</div>
	{/if}
</div>

<style>
	.ev-wrap {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 4px;
		max-width: 100%;
	}
	.ev-chip {
		display: flex; align-items: center; gap: var(--space-2);
		padding: var(--space-1) var(--space-3);
		font-size: var(--text-footnote);
		color: var(--text-tertiary);
		border-left: 2px solid var(--border-subtle);
		border-radius: 0 var(--radius-xs) var(--radius-xs) 0;
		background: none;
		max-width: 100%;
		overflow: hidden;
		text-decoration: none;
	}
	.ev-state {
		width: 14px;
		height: 14px;
		flex: 0 0 auto;
		fill: none;
		stroke: var(--text-tertiary);
		stroke-width: 1.75;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.ev-state.delivered { stroke: var(--green-text); }
	.ev-label {
		flex-shrink: 0;
		padding: 1px 6px;
		border-radius: var(--radius-xs);
		border: 1px solid var(--border-subtle);
		font-size: var(--text-caption);
		font-family: inherit;
		color: var(--text-secondary);
		background: var(--bg-secondary);
	}
	.ev-summary {
		flex: 1; min-width: 0;
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
		color: var(--text-secondary);
	}
	.ev-summary.collapsed {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		white-space: normal;
		text-overflow: clip;
	}
	.ev-summary.expanded {
		white-space: normal;
		overflow: visible;
		text-overflow: clip;
	}
	.ev-link {
		flex-shrink: 0;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		font-family: var(--font-mono);
		transition: color var(--dur-tight);
	}
	.ev-chip:hover .ev-link { color: var(--text-primary); }
	.long-message-toggle {
		align-self: flex-start;
		margin-left: 2px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-pill);
		background: var(--bg-secondary);
		color: var(--text-secondary);
		font: inherit;
		font-size: var(--text-caption);
		padding: 3px 9px;
		cursor: pointer;
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.long-message-toggle:hover {
		background: var(--bg-hover);
		color: var(--text-primary);
	}
	.ev-details-toggle {
		align-self: flex-start;
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		margin-left: var(--space-3);
		padding: 0;
		border: 0;
		background: transparent;
		color: var(--text-tertiary);
		font: inherit;
		font-size: var(--text-caption);
		cursor: pointer;
		transition: color var(--dur-tight) var(--ease-snap);
	}
	.ev-details-toggle:hover { color: var(--text-primary); }
	.ev-details-toggle svg {
		width: 12px;
		height: 12px;
		fill: none;
		stroke: currentColor;
		stroke-width: 2;
		stroke-linecap: round;
		stroke-linejoin: round;
		transition: transform var(--dur-tight) var(--ease-snap);
	}
	.ev-details-toggle svg.expanded { transform: rotate(180deg); }
	.ev-details {
		display: grid;
		gap: var(--space-1);
		margin: 0 0 0 var(--space-3);
		padding: var(--space-1) 0 0 var(--space-4);
		border-left: 1px solid var(--border-subtle);
		list-style: none;
		color: var(--text-secondary);
		font-size: var(--text-caption);
	}
	.ev-detail {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-3);
	}
	.ev-detail > span { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.ev-detail time {
		flex: 0 0 auto;
		color: var(--text-tertiary);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.ev-delivery {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		margin-left: var(--space-3);
		font-size: var(--text-caption);
		color: var(--text-tertiary);
	}
	.ev-delivery-status { font-style: italic; }
	.ev-delivery-action {
		/* Matches UserMessageBubble's delivery actions: bounded, separated
		   controls rather than adjacent underlined text that reads as one link. */
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 1px 7px;
		background: transparent;
		color: inherit;
		font: inherit;
		font-weight: 600;
		line-height: 1.5;
		cursor: pointer;
	}
	.ev-delivery-action:hover:not(:disabled) {
		background: var(--bg-tertiary);
		color: var(--text-primary, inherit);
	}
	.ev-delivery-action:disabled { cursor: wait; opacity: 0.45; }

	@media (pointer: coarse) {
		.ev-delivery-action { padding: 5px 11px; }
		.ev-details-toggle { min-height: 44px; }
	}
</style>
