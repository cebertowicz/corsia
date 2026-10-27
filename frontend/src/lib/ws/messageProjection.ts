import type { Message } from '../stores/threads';
import { readMessageDeliveryState } from '../messageDelivery';

/** Normalize singleton and batch public rows through the same wire contract. */
export function addedMessage(payload: any, threadId: string): Message | null {
	if (typeof payload?.id !== 'string' || typeof payload?.created_at !== 'string') return null;
	const revision = deliveryRevision(payload.delivery_revision);
	const deliveryState = readMessageDeliveryState(payload.delivery_state);
	const sequence = typeof payload.seq === 'number'
		&& Number.isInteger(payload.seq) && payload.seq > 0 ? payload.seq : undefined;
	return {
		id: payload.id,
		thread_id: threadId,
		role: payload.role,
		content: payload.content,
		metadata: payload.metadata,
		created_at: payload.created_at,
		...(sequence !== undefined ? { seq: sequence } : {}),
		...(payload.active_turn_id === null || typeof payload.active_turn_id === 'string'
			? { active_turn_id: payload.active_turn_id } : {}),
		...(payload.write_outcome === null || ['accepted', 'proved_no_write', 'ambiguous'].includes(payload.write_outcome)
			? { write_outcome: payload.write_outcome } : {}),
		...(deliveryState !== null
			? {
				delivery_state: deliveryState,
				delivery_error: typeof payload.delivery_error === 'string'
					? payload.delivery_error : null,
				...(revision !== undefined ? { delivery_revision: revision } : {}),
			}
			: {}),
	};
}

export function deliveryRevision(value: unknown): number | undefined {
	return typeof value === 'number' && Number.isInteger(value) && value >= 1
		? value
		: undefined;
}
