import { SvelteSet } from 'svelte/reactivity';
import type { ChatComment } from '$lib/stores/threads';
import type { Delivery } from '$lib/chatCommentTypes';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
import { deliveryPresentation, readMessageDeliveryState, type DeliveryPresentation, type MessageDeliveryState } from '$lib/messageDelivery';

/** The Retry chat offers a person's message in this state (UserMessageBubble), or null where it offers none; its title names the comment or reply. */
export type RetryControl = { label: 'Retry' | 'Retry anyway'; title: string };

/** A comment, or a reply in its thread, that the server stored and that is held or ended undelivered. */
export type UnsentSend = {
	messageId: string; note: string; createdAt: string; root: boolean;
	/** What chat shows for a message in this state: its label, hint and whether it carries the red `!`. */
	delivery: DeliveryPresentation;
	retry: RetryControl | null;
};

const ENDED: readonly MessageDeliveryState[] = ['failed', 'ambiguous', 'cancelled'];

/** Chat's labels for a queued input that waits on something other than a send in flight. */
const HELD_LABELS: ReadonlySet<DeliveryPresentation['label']> = new Set([
	'Choose a model to send', 'Waiting for account', 'Waiting for model', 'Switch to a local model to send', 'Waiting for checkpoint', 'Waiting to send', 'Not sent',
]);

type Noun = 'comment' | 'reply';

/**
 * The hint for a held comment or reply. Chat's own hints speak of "your message"; these name the comment or
 * reply. A hold with no wording of its own for comments shows its label alone.
 */
function heldTitle(label: DeliveryPresentation['label'], noun: Noun): string {
	if (label === 'Choose a model to send') return `This ${noun} is waiting for a model. Choose one for this chat to send it.`;
	if (label === 'Waiting for account') return `This ${noun} sends when the account is available.`;
	if (label === 'Waiting for model') return `This ${noun} sends when the model is available.`;
	if (label === 'Not sent') return `Couldn’t send this ${noun}. Try again.`;
	return label;
}

/** Sends whose Retry was refused for a cause no press can get past (their chat is deleted, their quote is gone). */
const refusedForGood = new SvelteSet<string>();

/** This send's Retry was refused for good: it keeps its label but offers no Retry it can't honour. */
export function retryRefusedForGood(messageId: string): void {
	refusedForGood.add(messageId);
}

function retryControl(state: MessageDeliveryState, delivery: DeliveryPresentation, noun: Noun): RetryControl | null {
	if (state === 'ambiguous') return { label: 'Retry anyway', title: `This may send the ${noun} twice if the provider received it before the connection failed` };
	// A queued send parked on a CORSIA defect moves only when the person retries it; Retry drives it in place.
	return state === 'failed' || delivery.label === 'Not sent' ? { label: 'Retry', title: `Send this ${noun} again` } : null;
}

function unsent(messageId: string, note: string, createdAt: string, root: boolean, send: Delivery): UnsentSend | null {
	const state = readMessageDeliveryState(send.delivery_state);
	if (!state || (state !== 'queued' && !ENDED.includes(state))) return null;
	const noun: Noun = root ? 'comment' : 'reply';
	const shown = deliveryPresentation({
		delivery_state: state, delivery_error: send.delivery_error ?? null, write_outcome: send.write_outcome ?? null,
		active_turn_id: send.active_turn_id ?? null, delivering_reason: send.delivering_reason ?? null, metadata: {},
	}, false);
	// A queued send in flight shows in its thread as a sent one; only a held one says so.
	if (!shown || (state === 'queued' && !HELD_LABELS.has(shown.label))) return null;
	const delivery = state === 'queued' ? { ...shown, title: heldTitle(shown.label, noun) } : shown;
	const retry = refusedForGood.has(messageId) ? null : retryControl(state, delivery, noun);
	return { messageId, note, createdAt, root, delivery, retry };
}

/**
 * What of this comment's thread is held or was not delivered, read from the server's delivery state on the
 * comment and on each reply the person wrote in it. Each keeps its text in the thread with chat's label for
 * its state, and chat's Retry where chat offers one. The server reports each send's newest Retry copy, so a
 * failure a Retry superseded leaves the comment once that copy is delivered, and comes back with Retry if the copy ends.
 */
export function unsentSends(comment: ChatComment): UnsentSend[] {
	const sends = [unsent(comment.user_message_id, comment.note, comment.created_at, true, comment)];
	for (const answer of comment.answers) {
		if (answer.author_kind === 'user') sends.push(unsent(answer.user_message_id, answer.text, answer.created_at, false, answer));
	}
	return sends.filter((send): send is UnsentSend => send !== null);
}

/** Whether a send in this thread needs the person, by chat's red-marker rule. */
export function needsAttention(comment: ChatComment): boolean {
	return unsentSends(comment).some(send => send.delivery.failed);
}

/** A reply typed in this comment's thread is on its way to the server. */
export function replySending(comment: ChatComment): boolean {
	return !!chatCommentDrafts.reply(comment.id)?.posting;
}
