<script lang="ts">
	/**
	 * One input the user deliberately parked until the running turn finishes.
	 *
	 * A steer and a queue are opposite instructions and must not look alike. A
	 * steer is going into the step running now, so it stays in the canvas as the
	 * sent bubble it is about to become. A queue is the user saying "not yet": it
	 * has not been sent, nothing is sending it, and it does not belong among
	 * messages that have. So it leaves the transcript entirely and waits on the
	 * composer — the surface the user is holding — in the sky tone the hold-to-
	 * queue gesture already fills the Send button with, marked with a clock, in
	 * the FIFO order the backend will drain it in, with the control that takes it
	 * back out of the line sitting on the row it removes.
	 *
	 * Position, colour and glyph carry the state. Nothing here depends on the
	 * user reading a caption to know this message is NOT about to be sent.
	 *
	 * Styling is the `.queued-*` block in `InputBar.css`, restored with the
	 * markup it was written for (it was orphaned when `df2f9d19` deleted this
	 * stack). Like every other composer part this component takes its rules from
	 * that sheet rather than carrying a private copy.
	 */
	import { requestDeliveryAction } from '$lib/deliveryActions';
	import { deliveryPresentation } from '$lib/messageDelivery';
	import { SWITCH_TO_LOCAL_MODEL, WAITING_FOR_ACCOUNT, WAITING_FOR_MODEL, WAITING_TO_SEND } from '$lib/messageDeliveryCopy';
	import { limitHeldThreadId } from '$lib/stores/overageSetting';
	import { token } from '$lib/stores/auth';
	import { activeThreadId, isThreadTurnActive, type Message } from '$lib/stores/threads';
	import { turnStates } from '$lib/ws/turnState';

	let {
		msg,
		position = 0,
	}: {
		msg: Message;
		/** 1-based place in the queue; 0 when this is the only one waiting. */
		position?: number;
	} = $props();

	let cancelPending = $state(false);

	const attachmentCount = $derived(
		Array.isArray(msg.metadata?.attachments) ? msg.metadata.attachments.length : 0,
	);
	// One line, so the composer keeps its own room. The full text is one hover
	// (or one delivery) away, and the thread it lands in is where it belongs.
	const summary = $derived(msg.content?.trim() || 'Attachment');
	const messageThreadId = $derived(
		typeof msg.thread_id === 'string' && msg.thread_id ? msg.thread_id : $activeThreadId,
	);
	const messageTurnActive = $derived(isThreadTurnActive($turnStates, messageThreadId));
	const delivery = $derived(deliveryPresentation(msg, messageTurnActive, messageThreadId !== null && messageThreadId === $limitHeldThreadId));
	// Removing needs a durable row to remove. `delivery_state` arrives only
	// with server frames, and between the send and its receipt there is
	// nothing on the server to address, so the control is visibly disabled
	// rather than absent — a chip whose ✕ appears a moment later is a control
	// the user's thumb was already moving toward.
	const canCancel = $derived(
		msg.delivery_state === 'queued' && !!messageThreadId && !!$token,
	);

	async function cancelQueued(): Promise<void> {
		const threadId = messageThreadId;
		const authToken = $token;
		if (!canCancel || !threadId || !authToken || cancelPending) return;
		cancelPending = true;
		try {
			await requestDeliveryAction('cancel', {
				threadId,
				messageId: msg.id,
				authToken,
				failureText: 'Could not take this message out of the queue. Try again.',
			});
		} finally {
			cancelPending = false;
		}
	}
</script>

<li class="queued-chip" title={delivery?.title}>
	{#if position > 0}
		<span class="queued-position" aria-hidden="true">{position}</span>
	{/if}
	<svg
		class="queued-glyph" width="13" height="13" viewBox="0 0 24 24" fill="none"
		stroke="currentColor" stroke-width="2" stroke-linecap="round"
		stroke-linejoin="round" aria-hidden="true"
	><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
	<span class="queued-text" title={summary}>
		{delivery?.label === 'Waiting for checkpoint' || delivery?.label === WAITING_FOR_MODEL.label || delivery?.label === SWITCH_TO_LOCAL_MODEL || delivery?.label === WAITING_FOR_ACCOUNT || delivery?.label === WAITING_TO_SEND.label ? `${delivery.label}: ${summary}` : summary}
	</span>
	{#if attachmentCount > 0}
		<span class="queued-attach" aria-label={`${attachmentCount} attached`}>
			<svg
				width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
				stroke-width="2" stroke-linecap="round" aria-hidden="true"
			><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
			{attachmentCount}
		</span>
	{/if}
	<button
		type="button"
		class="queued-remove"
		data-delivery-action="cancel"
		disabled={!canCancel || cancelPending}
		aria-label={`Remove queued message: ${summary}`}
		title="Take this message out of the queue"
		onclick={() => { void cancelQueued(); }}
	>
		<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>
	</button>
</li>
