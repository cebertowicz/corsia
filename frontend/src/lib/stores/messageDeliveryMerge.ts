import type { Message } from './threads';

function validDeliveryRevision(value: unknown): number | null {
	return typeof value === 'number' && Number.isInteger(value) && value >= 1
		? value
		: null;
}

/** Preserve the newest delivery transition when an older server row arrives later. */
export function mergeMessageDelivery(existing: Message, incoming: Message): Message {
	const existingRevision = validDeliveryRevision(existing.delivery_revision);
	const incomingRevision = validDeliveryRevision(incoming.delivery_revision);
	if (
		existingRevision !== null
		&& incomingRevision !== null
		&& incomingRevision <= existingRevision
		&& existing.delivery_state !== undefined
	) {
		return {
			...existing,
			...incoming,
			delivery_state: existing.delivery_state,
			delivery_error: existing.delivery_error,
			delivery_revision: existing.delivery_revision,
			write_outcome: existing.write_outcome,
			...(incomingRevision < existingRevision
				? { active_turn_id: existing.active_turn_id, seq: existing.seq } : {}),
		};
	}
	return { ...existing, ...incoming };
}
