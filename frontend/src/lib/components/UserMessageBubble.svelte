<script lang="ts">
	import { token } from '$lib/stores/auth';
	import { activeThreadId, isThreadTurnActive } from '$lib/stores/threads';
	import { turnStates } from '$lib/ws/turnState';
	import { formatTime } from '$lib/time';
	import { renderMarkdown } from '$lib/markdown';
	import { isLongMessageText } from '$lib/messageCollapse';
	import { requestDeliveryAction, type DeliveryAction } from '$lib/deliveryActions';
	import {
		deliveryPresentation,
		isParkedOnOurDefect,
		isProviderPendingApplyNow,
		isQueuedInputOwnedByActiveTurn,
	} from '$lib/messageDelivery';
	import { limitHeldThreadId } from '$lib/stores/overageSetting';
	import AgentMessageAttribution from './AgentMessageAttribution.svelte';
	import UserBubbleAttachments from './UserBubbleAttachments.svelte';

	let {
		msg,
		onOpenLightbox,
		deliveryOnly = false,
	}: {
		msg: any;
		onOpenLightbox: (url: string) => void;
		deliveryOnly?: boolean;
	} = $props();

	let contentExpanded = $state(false);
	let renderedMsgId = $state<string | null>(null);
	let deliveryActionPending = $state<string | null>(null);

	$effect(() => {
		if (msg.id !== renderedMsgId) {
			contentExpanded = false;
			renderedMsgId = msg.id;
		}
	});

	const isAgentSent = $derived(msg.metadata?.sender_kind === 'agent');
	const isRoutedEventInput = $derived(!!msg.metadata?.routed_event);
	const messageThreadId = $derived(
		typeof msg.thread_id === 'string' && msg.thread_id ? msg.thread_id : $activeThreadId,
	);
	const contentIsLong = $derived(isLongMessageText(msg.content));
	const contentCollapsed = $derived(contentIsLong && !contentExpanded);
	const deliveryState = $derived(
		msg._optimistic === true ? 'sending' : (msg.delivery_state ?? null),
	);
	const activeTurnOwnsInput = $derived(isQueuedInputOwnedByActiveTurn(msg));
	const messageTurnActive = $derived(isThreadTurnActive($turnStates, messageThreadId));
	const delivery = $derived(deliveryPresentation(msg, messageTurnActive, messageThreadId !== null && messageThreadId === $limitHeldThreadId));
	const providerPendingApplyNow = $derived(isProviderPendingApplyNow(msg, messageTurnActive));
	const deliveryActionState = $derived(activeTurnOwnsInput ? null : deliveryState);
	const timestampPending = $derived(
		deliveryState === 'queued' || deliveryState === 'sending',
	);
	const deliveryLabel = $derived(delivery?.label ?? null);
	// Delivery status derives from the canonical projection.
	const deliveryTitle = $derived(delivery?.title ?? null);
	const deliveryFailed = $derived(delivery?.failed === true);
	const parkedOnOurDefect = $derived(isParkedOnOurDefect(msg));
	// Codes that mean "we do not know whether the provider got it". A resend may
	// duplicate, so the control says so rather than pretending it is free.
	const providerWriteAmbiguous = $derived(
		deliveryState === 'ambiguous',
	);
	// An agent send is a message like any other once it is queued: it can park
	// on our own defect, and a parked head's only exit is this control. Hiding
	// it left agent rows with no exit at all -- the backend's own escape names
	// the user's explicit retry, and there was nothing for the user to press.
	// Routed events stay excluded: their retry endpoint rejects non-user roles,
	// so showing a control that cannot work would be a worse lie than none.
	const canManageDelivery = $derived(
		!isRoutedEventInput && !!messageThreadId && !!$token,
	);

	async function runDeliveryAction(action: DeliveryAction): Promise<void> {
		const threadId = messageThreadId;
		const authToken = $token;
		if (!threadId || !authToken || deliveryActionPending) return;
		deliveryActionPending = action;
		try {
			await requestDeliveryAction(action, {
				threadId,
				messageId: msg.id,
				authToken,
				failureText: action === 'retry'
					? 'Could not send this again. Try once more.'
					: 'Could not cancel this message. Try again.',
			});
		} finally {
			deliveryActionPending = null;
		}
	}
</script>

<div class="user-bubble-wrapper" class:agent-sent={isAgentSent} class:provider-pending={providerPendingApplyNow}>
	<div class="bubble-col">
		{#if isAgentSent && !deliveryOnly}
			<AgentMessageAttribution {msg} />
		{/if}
		<div class="user-bubble">
			{#if !deliveryOnly}
			{#if msg.metadata?.attachments?.length > 0}
				<UserBubbleAttachments
					attachments={msg.metadata.attachments}
					{onOpenLightbox}
				/>
			{/if}
			{#if msg.content}
				<div class="bubble-content long-message-body" class:collapsed={contentCollapsed} class:md-content={isAgentSent}>
					{#if isAgentSent}
						{@html renderMarkdown(msg.content)}
					{:else}
						{msg.content}
					{/if}
				</div>
				{#if contentIsLong}
					<button
						class="long-message-toggle"
						aria-expanded={contentExpanded}
						onclick={() => (contentExpanded = !contentExpanded)}
					>
						{contentExpanded ? 'Show less' : 'Show more'}
					</button>
				{/if}
			{/if}
			{/if}
			<div class="msg-time">
				{#if deliveryFailed}
					<!-- The failure is marked where the user is looking. A failure
					     they have to go looking for is one they will not see. -->
					<span class="delivery-failed-mark" title={deliveryTitle} aria-label="Needs your attention">!</span>
				{/if}
				{#if deliveryLabel}
					<span
						class="delivery-status"
						class:terminal={deliveryState === 'ambiguous'
							|| deliveryState === 'cancelled'
							|| deliveryState === 'failed'}
						class:sending={deliveryLabel === 'Sending when ready…'}
						title={deliveryTitle}
					>{deliveryLabel}</span>
				{/if}
				{#if deliveryActionState === 'queued' && canManageDelivery}
					{#if parkedOnOurDefect}
						<!-- Nothing automatic will move this one: a CORSIA defect is
						     a bug to fix, not a condition to retry. The user is the
						     only exit, so the exit has to be here. -->
						<button
							type="button"
							class="delivery-action"
							data-delivery-action="retry"
							disabled={deliveryActionPending !== null}
							title="Try sending this message again"
							onclick={() => { void runDeliveryAction('retry'); }}
						>Retry</button>
					{/if}
					<button
						type="button"
						class="delivery-action"
						data-delivery-action="cancel"
						disabled={deliveryActionPending !== null}
						title="Keep this message visible, but do not deliver it"
						onclick={() => { void runDeliveryAction('cancel'); }}
					>Cancel</button>
				{:else if (deliveryActionState === 'failed' || deliveryActionState === 'ambiguous') && canManageDelivery}
					<button
						type="button"
						class="delivery-action"
						data-delivery-action={providerWriteAmbiguous ? 'retry-anyway' : 'retry'}
						disabled={deliveryActionPending !== null}
						title={providerWriteAmbiguous
						? 'This may send the message twice if the provider received it before the connection failed'
						: 'Send this message again'}
						onclick={() => { void runDeliveryAction('retry'); }}
					>{providerWriteAmbiguous ? 'Retry anyway' : 'Retry'}</button>
				{/if}
				{#if !timestampPending}
					{formatTime(msg.created_at)}
				{/if}
			</div>
		</div>
	</div>
</div>

<style>
		.user-bubble-wrapper.provider-pending .user-bubble {
			box-shadow: inset 0 0 0 1px rgba(255,255,255,0.24);
		}
		.delivery-status { font-style: italic; }
		.delivery-failed-mark {
			display: inline-flex;
			align-items: center;
			justify-content: center;
			width: 15px;
			height: 15px;
			border-radius: var(--radius-circle, 50%);
			background: var(--red);
			color: white;
			font-size: var(--text-caption);
			font-style: normal;
			font-weight: 800;
			line-height: 1;
		}
		.delivery-status.sending { animation: pending-status 1.8s ease-in-out infinite; }
		@keyframes pending-status { 50% { opacity: 0.58; } }
		@media (prefers-reduced-motion: reduce) {
			.delivery-status.sending { animation: none; }
		}
	.user-bubble-wrapper {
		display: flex; align-items: flex-start; gap: 6px;
		justify-content: flex-end; max-width: 75%;
		margin-left: auto;
	}
	.bubble-col {
		display: flex; flex-direction: column; align-items: flex-end; min-width: 0;
	}
	.user-bubble-wrapper.agent-sent { justify-content: flex-start; margin-left: 0; }
	.user-bubble-wrapper.agent-sent .bubble-col { align-items: flex-start; }
	.user-bubble-wrapper.agent-sent .user-bubble {
		border-radius: 4px 18px 18px 18px;
		background: var(--bg-secondary);
		color: var(--text-primary);
	}
	.user-bubble-wrapper.agent-sent .msg-time { color: var(--text-tertiary); }
	.delivery-status {
		margin-right: 6px;
		font-weight: 600;
	}
	.delivery-status.sending { opacity: 0.76; }
	.delivery-status.terminal { color: #ffe2a0; }
	.delivery-action {
		/* These were borderless underlined text sitting 4px apart, so two of
		   them read as a single underlined run — "RetryCancel" — and neither
		   offered a tap target worth aiming at. Give each one its own bounds. */
		border: 1px solid var(--border, rgba(255,255,255,0.24));
		border-radius: var(--radius-sm, 6px);
		padding: 1px 7px;
		background: transparent;
		color: inherit;
		font: inherit;
		font-weight: 600;
		line-height: 1.5;
		cursor: pointer;
		opacity: 0.9;
	}
	.delivery-action + .delivery-action { margin-left: 4px; }
	.delivery-action:hover:not(:disabled) {
		opacity: 1;
		background: var(--bg-tertiary, rgba(255,255,255,0.1));
	}
	.delivery-action:disabled { cursor: wait; opacity: 0.45; }
	.user-bubble {
		background: var(--accent); color: white;
		border-radius: 18px 18px 4px 18px; padding: 10px 16px;
		min-width: 0; max-width: 100%; overflow: hidden;
	}
	.bubble-content { font-size: var(--text-callout); line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
	.long-message-body.collapsed {
		max-height: 14rem;
		overflow: hidden;
		mask-image: linear-gradient(to bottom, black 72%, transparent 100%);
		-webkit-mask-image: linear-gradient(to bottom, black 72%, transparent 100%);
	}
	.long-message-toggle {
		margin-top: 8px;
		border: 1px solid rgba(255,255,255,0.2);
		border-radius: var(--radius-pill);
		background: rgba(0,0,0,0.16);
		color: currentColor;
		font: inherit;
		font-size: var(--text-footnote);
		padding: 4px 10px;
		cursor: pointer;
		opacity: 0.85;
		transition: background var(--dur-tight), opacity var(--dur-tight);
	}
	.long-message-toggle:hover {
		background: rgba(0,0,0,0.24);
		opacity: 1;
	}
	.user-bubble-wrapper.agent-sent .long-message-toggle {
		border-color: var(--border-subtle);
		background: var(--bg-tertiary);
		color: var(--text-secondary);
	}
	.user-bubble-wrapper.agent-sent .long-message-toggle:hover {
		background: var(--bg-hover);
		color: var(--text-primary);
	}
	.msg-time { font-size: var(--text-caption); color: rgba(255,255,255,0.6); margin-top: 4px; opacity: 0.7; display: flex; align-items: center; gap: 4px; justify-content: flex-end; }
	@media (pointer: coarse) {
		/* Touch needs a real target: these sat at caption size with no padding,
		   so adjacent actions were a coin flip to hit. */
		.delivery-action { padding: 5px 11px; }
		.delivery-action + .delivery-action { margin-left: 6px; }
	}

	@media (max-width: 768px) {
		.user-bubble-wrapper { max-width: 85%; }
	}
</style>
