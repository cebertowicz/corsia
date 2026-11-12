import type { ChatComment } from '$lib/stores/threads';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
import { deliveryPresentation, readMessageDeliveryState, type DeliveryPresentation, type MessageDeliveryState } from '$lib/messageDelivery';

/** The Retry chat offers a person's message in this state (UserMessageBubble), or null where it offers none; its title names the comment. */
export type RetryControl = { label: 'Retry' | 'Retry anyway'; title: string };

/** A comment, or a reply in its thread, that the server stored and whose delivery to the agent ended undelivered. */
export type EndedSend = {
	messageId: string; note: string; createdAt: string; root: boolean;
	/** What chat shows for a message in this state: its label, hint and whether it carries the red `!`. */
	delivery: DeliveryPresentation;
	retry: RetryControl | null;
};

const ENDED: readonly MessageDeliveryState[] = ['failed', 'ambiguous', 'cancelled'];

function retryControl(state: MessageDeliveryState): RetryControl | null {
	if (state === 'ambiguous') return { label: 'Retry anyway', title: 'This may send the comment twice if the provider received it before the connection failed' };
	return state === 'failed' ? { label: 'Retry', title: 'Send this comment again' } : null;
}

function ended(messageId: string, note: string, createdAt: string, root: boolean, rawState: unknown, error: string | null | undefined): EndedSend | null {
	const state = readMessageDeliveryState(rawState);
	if (!state || !ENDED.includes(state)) return null;
	const delivery = deliveryPresentation({ delivery_state: state, delivery_error: error ?? null }, false);
	return delivery && { messageId, note, createdAt, root, delivery, retry: retryControl(state) };
}

/**
 * What of this comment's thread was not delivered, read from the server's delivery state on the comment and
 * on each reply the person wrote in it. Each keeps its text in the thread with chat's label for its state,
 * and chat's Retry where chat offers one. A Retry takes the ended comment's place on the server, so a
 * failure it superseded is no longer on the comment.
 */
export function endedSends(comment: ChatComment): EndedSend[] {
	const sends = [ended(comment.user_message_id, comment.note, comment.created_at, true, comment.delivery_state, comment.delivery_error)];
	for (const answer of comment.answers) {
		if (answer.author_kind === 'user') sends.push(ended(answer.user_message_id, answer.text, answer.created_at, false, answer.delivery_state, answer.delivery_error));
	}
	return sends.filter((send): send is EndedSend => send !== null);
}

/** Whether a send in this thread needs the person, by chat's red-marker rule. */
export function needsAttention(comment: ChatComment): boolean {
	return endedSends(comment).some(send => send.delivery.failed);
}

/** A reply typed in this comment's thread is on its way to the server. */
export function replySending(comment: ChatComment): boolean {
	return !!chatCommentDrafts.reply(comment.id)?.posting;
}
