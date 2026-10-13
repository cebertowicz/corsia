import {
	blockedReason,
	NO_MODEL_SELECTED,
	SELECTED_MODEL_UNAVAILABLE,
	SET_ASIDE_COPY,
	UNCONFIRMED_TITLE,
	WAITING_FOR_MODEL,
	WAITING_FOR_MODEL_CHOICE,
} from './messageDeliveryCopy';
import type { Message } from './stores/threads';

const MESSAGE_DELIVERY_STATES = [
	'queued', 'delivered', 'ambiguous', 'cancelled', 'failed',
] as const;

export type MessageDeliveryState = typeof MESSAGE_DELIVERY_STATES[number];

export function readMessageDeliveryState(value: unknown): MessageDeliveryState | null {
	return typeof value === 'string'
		&& (MESSAGE_DELIVERY_STATES as readonly string[]).includes(value)
		? value as MessageDeliveryState
		: null;
}

export type DeliveryPresentation = {
	label:
		| 'Sending when ready…'
		| 'Waiting for model'
		| 'Choose a model to send'
		| 'Waiting for checkpoint'
		| 'Delivery uncertain'
		| 'Queued'
		| 'Cancelled by you'
		| 'Held while paused'
		| 'Not needed'
		| 'Not sent'
		| `Not delivered: ${string}`;
	title: string;
	pending: true;
	/**
	 * Whether this message carries the red `!`.
	 *
	 * True exactly when the message will not move without the user. A failed
	 * message is a visible dead message, never a wedged thread: the marker says
	 * "this one needs you", and the thread keeps taking new input either way.
	 */
	failed: boolean;
};

/** Model holds by cause; the copy is the same for every provider. */
const MODEL_HOLD_COPY: Readonly<Record<string, Pick<DeliveryPresentation, 'label' | 'title'>>> = Object.freeze({
	[SELECTED_MODEL_UNAVAILABLE]: WAITING_FOR_MODEL,
	[NO_MODEL_SELECTED]: WAITING_FOR_MODEL_CHOICE,
});

type DeliveryMessage = Pick<
	Message,
	| 'delivery_state'
	| 'delivery_error'
	| 'active_turn_id'
	| 'write_outcome'
	| 'metadata'
	| '_optimistic'
	| 'delivering_reason'
> & {
	/** Which thread the row belongs to; the pre-receipt echo arm reads it. */
	thread_id?: Message['thread_id'];
};

/** Identifies an accepted apply-now input already owned by the active turn. */
export function isQueuedInputOwnedByActiveTurn(
	message: DeliveryMessage,
): boolean {
	return message.delivery_state === 'queued'
		&& typeof message.active_turn_id === 'string'
		&& message.active_turn_id.length > 0;
}

/**
 * Whether the user explicitly asked this input to wait out the running turn.
 *
 * A Queue is the user's own instruction, so it is true before any receipt: the
 * optimistic echo already carries the intent (`ws/send.ts` stamps it at send
 * time), and rendering that echo as an ordinary sent message for one round trip
 * is the same false "already delivered" claim the pending region exists to
 * prevent. An ended queue — delivered, cancelled, refused — is no longer
 * waiting and reads at the sequence position its ending earned it.
 */
export function isQueuedAfterTurn(message: DeliveryMessage): boolean {
	if (isQueuedInputOwnedByActiveTurn(message)) return false;
	if (message.metadata?.delivery_intent !== 'queue_after_turn') return false;
	if (message.delivery_state === 'queued') return true;
	return message._optimistic === true && message.delivery_state === undefined;
}

/**
 * Whether this input has not yet earned a place in the delivered sequence.
 *
 * A queued input has no delivered transcript sequence yet. This predicate keeps
 * it out of the ordered transcript until the server assigns that position,
 * whether it is waiting for a provider boundary or for an explicit Queue.
 *
 * The one pre-receipt claim the client may make is its own echo of a send into
 * a running turn: nothing on the server has said anything about that row yet,
 * but the turn it must wait out is live, so the echo holds the waiting region
 * until the first receipt replaces the claim with server truth. That claim is
 * derived here — echo plus live turn — and never written into
 * `delivery_state`, which stays server truth alone.
 *
 * It is a "keep out of sequence" test, not a "render here" test. Where such an
 * input goes instead is `isImminentDelivery`'s question.
 *
 * The one input excluded is an apply-now message the active turn has already
 * claimed: the provider holds it now, so it reads in place rather than waiting.
 */
export function isAwaitingDelivery(
	message: DeliveryMessage,
	threadTurnActive: boolean,
): boolean {
	const unreceiptedEchoOfRunningTurn = message._optimistic === true
		&& typeof message.thread_id === 'string'
		&& threadTurnActive;
	return (unreceiptedEchoOfRunningTurn
		|| message.delivery_state === 'queued'
		|| isQueuedAfterTurn(message))
		&& !isQueuedInputOwnedByActiveTurn(message);
}

/**
 * Whether this input is going into the step that is running right now.
 *
 * Membership of the in-canvas pending region, which means one thing only:
 * imminent delivery. Everything `isAwaitingDelivery` holds qualifies EXCEPT the
 * inputs the user deliberately parked until the turn ends — those are not about
 * to be sent, so they leave the canvas altogether and wait on the composer.
 *
 * The split is the point: a user has to be able to tell what is about to happen
 * from what is deliberately not about to happen without reading the caption
 * under it, and two things that share a region read as one thing.
 */
export function isImminentDelivery(
	message: DeliveryMessage,
	threadTurnActive: boolean,
): boolean {
	return isAwaitingDelivery(message, threadTurnActive) && !isQueuedAfterTurn(message);
}

/**
 * Identifies an imminent input that nothing has gone wrong with yet.
 *
 * Narrower than `isImminentDelivery` by the two states that have their own
 * descriptors: an optimistic echo has no server truth to report, and a failed
 * delivery is not being sent at all.
 */
export function isProviderPendingApplyNow(
	message: DeliveryMessage,
	threadTurnActive: boolean,
): boolean {
	return message._optimistic !== true
		&& message.delivery_error == null
		&& isImminentDelivery(message, threadTurnActive);
}

/**
 * The reason a queued input carries while it is parked on a CORSIA defect.
 *
 * Mirrors `core.delivery_reactor.failure_side.OUR_DEFECT_REASON`. Retry exists
 * for provider conditions that may clear; a bug of ours is not one, so nothing
 * automatic will move this input. That makes the user the only exit, which is
 * why it renders as a failure with working controls instead of as "sending".
 */
const OUR_DEFECT_REASON = 'delivery_defect';

/** Whether this queued input is waiting on a CORSIA defect, not a provider. */
export function isParkedOnOurDefect(message: DeliveryMessage): boolean {
	return message.delivery_state === 'queued'
		&& message.delivery_error == null
		&& message.delivering_reason === OUR_DEFECT_REASON;
}

/**
 * Maps durable delivery truth to the limited set of user-visible descriptors.
 *
 * `boundModelLimited` is read-time state, not a delivery fact: the thread's
 * bound model row is usage-limited, so admission holds this input until the
 * limit clears or the person switches model.
 */
export function deliveryPresentation(
	message: DeliveryMessage,
	threadTurnActive: boolean,
	boundModelLimited = false,
): DeliveryPresentation | null {
	const heldForRecovery = message.delivery_state === 'queued'
		&& message.write_outcome === 'proved_no_write' && !isQueuedInputOwnedByActiveTurn(message);
	const modelHold = heldForRecovery && typeof message.delivery_error === 'string'
		? MODEL_HOLD_COPY[message.delivery_error]
		: undefined;
	if (modelHold) {
		return {
			...modelHold,
			pending: true,
			failed: false,
		};
	}
	if (heldForRecovery) {
		return {
			label: 'Waiting for checkpoint',
			title: 'The checkpoint needed to continue is unavailable. Your message is saved and has not been sent.',
			pending: true,
			failed: false,
		};
	}
	// Ahead of the optimistic guard on purpose. Every other descriptor reports
	// something only the server knows; this one reports what the user just asked
	// for, which is true from the moment they ask it.
	if (isQueuedAfterTurn(message)) {
		// No turn is left to wait for: the limit stopped it, and now holds this.
		if (boundModelLimited && !threadTurnActive) return { ...WAITING_FOR_MODEL, pending: true, failed: false };
		return {
			label: 'Queued',
			title: 'Queued by request until the current turn completes',
			pending: true,
			failed: false,
		};
	}
	if (message._optimistic === true || message.delivery_state === 'delivered') return null;
	if (message.delivery_state === 'ambiguous') {
		return {
			label: 'Delivery uncertain',
			title: UNCONFIRMED_TITLE,
			pending: true,
			failed: true,
		};
	}
	if (message.delivery_state === 'cancelled') {
		const setAside = typeof message.delivery_error === 'string'
			? SET_ASIDE_COPY[message.delivery_error]
			: undefined;
		if (setAside) return { ...setAside, pending: true, failed: false };
		return {
			label: 'Cancelled by you',
			title: 'Cancelled by you',
			pending: true,
			failed: false,
		};
	}
	if (message.delivery_state === 'failed') {
		const reason = blockedReason(message.delivery_error);
		return {
			label: `Not delivered: ${reason}`,
			title: reason,
			pending: true,
			failed: true,
		};
	}
	// Before the "sending" arm on purpose: nothing is sending this and nothing
	// will. Saying otherwise is the silence that made these inputs show pending
	// forever while never arriving and never failing.
	if (isParkedOnOurDefect(message)) {
		return {
			label: 'Not sent',
			title: 'CORSIA hit a problem sending this and stopped instead of '
				+ 'retrying a send it knows is broken. Your message is still here — '
				+ 'retry it, or cancel it.',
			pending: true,
			failed: true,
		};
	}
	if (isProviderPendingApplyNow(message, threadTurnActive)) {
		if (boundModelLimited) return { ...WAITING_FOR_MODEL, pending: true, failed: false };
		return {
			label: 'Sending when ready…',
			title: 'CORSIA will send this as soon as the thread can continue.',
			pending: true,
			failed: false,
		};
	}
	return null;
}
