import { plainErrorText } from './plainError';
import {
	mergeMessageDelivery,
	messages,
	type Message,
} from './stores/threads';
import { isDisplayedThread } from './stores/displayedThreads';
import { toasts } from './stores/toast';

/**
 * The two exits a user has from a message that has not been delivered.
 *
 * There is one implementation because there is one endpoint pair. Three
 * surfaces offer these controls — the user bubble, the queued input chip, and
 * the system-wake event chip — and each used to carry its own copy of the
 * fetch, the failure copy, and the adopt-the-canonical-row step. Copies drift:
 * the wake chip printed raw `detail` text where the bubble ran it through
 * `plainErrorText`, so the same rejection reached the user as a sentence on one
 * surface and as a machine code on another.
 */
export type DeliveryAction = 'cancel' | 'retry';

export type DeliveryActionRequest = {
	threadId: string;
	messageId: string;
	authToken: string;
	/** What to say when the server gave no sentence a human wrote. */
	failureText: string;
};

/**
 * Adopt the canonical row a delivery action returned.
 *
 * Guarded on every identity in the payload: the thread is still the one on
 * screen, the envelope is for that thread, and the row is the one acted on.
 * `mergeMessageDelivery` then keeps the newer revision, so a slow response
 * cannot overwrite delivery truth a websocket event already advanced.
 */
function adoptDeliveryResponse(
	threadId: string,
	messageId: string,
	payload: unknown,
): void {
	if (
		!isDisplayedThread(threadId)
		|| !payload
		|| typeof payload !== 'object'
		|| (payload as { thread_id?: unknown }).thread_id !== threadId
	) return;
	const canonical = (payload as { message?: unknown }).message;
	if (
		!canonical
		|| typeof canonical !== 'object'
		|| (canonical as { id?: unknown }).id !== messageId
	) return;
	messages.update(current => current.map(existing =>
		existing.id === messageId
			? mergeMessageDelivery(existing, canonical as Message)
			: existing,
	));
}

/**
 * Ask the server to cancel or retry one message's delivery.
 *
 * Resolves once the outcome is on screen either way: the canonical row on
 * success, a toast the user can read on failure. Callers own only their own
 * in-flight flag.
 */
export async function requestDeliveryAction(
	action: DeliveryAction,
	{ threadId, messageId, authToken, failureText }: DeliveryActionRequest,
): Promise<void> {
	try {
		const response = await fetch(
			`/api/threads/${encodeURIComponent(threadId)}`
			+ `/messages/${encodeURIComponent(messageId)}`
			+ `/${action}?token=${encodeURIComponent(authToken)}`,
			{ method: 'POST' },
		);
		if (!response.ok) {
			const payload = await response.json().catch(() => null);
			if (payload?.detail !== undefined) {
				console.error('[delivery] action rejected', action, payload.detail);
			}
			throw new Error(plainErrorText(payload?.detail, failureText));
		}
		adoptDeliveryResponse(
			threadId,
			messageId,
			await response.json().catch(() => null),
		);
	} catch (error) {
		toasts.show(error instanceof Error ? error.message : failureText, 'error');
	}
}
