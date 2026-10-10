import { isParkedOnOurDefect } from '$lib/messageDelivery';
import type { TranscriptMessage } from './messageListModel';

/**
 * A wake that needs a Retry: delivery failed, is uncertain, or is parked on a
 * CORSIA defect. The one home of this condition: EventChip offers its Retry under
 * it, and Focus keeps this context update out of the blocks because the pending
 * area is where its Retry lives.
 */
export function needsWakeRetry(msg: TranscriptMessage): boolean {
	return msg.role === 'system'
		&& msg.metadata?.event_disposition === 'wake'
		&& (msg.delivery_state === 'failed' || msg.delivery_state === 'ambiguous'
			|| (msg.delivery_state === 'queued' && isParkedOnOurDefect(msg)));
}

/** The Retry control's action, label and hint; an uncertain delivery offers "Retry anyway". */
export function wakeRetryControl(msg: TranscriptMessage): { action: 'retry' | 'retry-anyway'; label: string; title: string } {
	return msg.delivery_state === 'ambiguous'
		? { action: 'retry-anyway', label: 'Retry anyway', title: 'Try again even though the earlier delivery may still arrive' }
		: { action: 'retry', label: 'Retry', title: 'Try delivering this wake again' };
}

/** What the wake said, in one line. */
export function wakeSummary(msg: TranscriptMessage): string {
	const text = String(msg.metadata?.summary || msg.content || 'Context update');
	return text.split('\n').find((line) => line.trim())?.trim() ?? 'Context update';
}
