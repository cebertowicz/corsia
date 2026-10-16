<script lang="ts">
	import { requestDeliveryAction } from '$lib/deliveryActions';
	import { token } from '$lib/stores/auth';
	import type { TranscriptMessage } from './messageListModel';
	import { wakeRetryControl, wakeSummary } from './pendingWakeRetry';

	// Focus's pending area for a context update that could not be delivered: one line, one Retry.
	let { msg, threadId }: { msg: Record<string, any>; threadId: string | null | undefined } = $props();

	let pending = $state(false);
	const control = $derived(wakeRetryControl(msg as TranscriptMessage));

	async function retry(): Promise<void> {
		const authToken = $token;
		if (!threadId || !authToken || pending) return;
		pending = true;
		try {
			await requestDeliveryAction('retry', { threadId, messageId: msg.id, authToken, failureText: 'Could not retry this wake' });
		} finally {
			pending = false;
		}
	}
</script>

<div class="wake-retry" data-wake-retry>
	<span class="text">Couldn't deliver: {wakeSummary(msg)} ·</span>
	<button
		type="button"
		class="retry"
		data-delivery-action={control.action}
		disabled={pending}
		title={control.title}
		onclick={() => { void retry(); }}
	>{control.label}</button>
</div>

<style>
	.wake-retry { display: flex; align-items: baseline; gap: 6px; max-width: 760px; margin: 0 auto 12px; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.text { min-width: 0; overflow-wrap: anywhere; }
	.retry { flex: 0 0 auto; padding: 0; border: 0; background: transparent; color: var(--accent-text); font: inherit; cursor: pointer; }
	.retry:disabled { cursor: wait; opacity: 0.45; }
	@media (pointer: coarse) { .retry { min-height: 44px; padding: 0 8px; } }
</style>
