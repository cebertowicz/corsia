import { emitProductEvent } from '$lib/product_events';
import type { ChatComment } from '$lib/chatCommentAnchors';
import { sendMessage } from '$lib/ws';
import { toasts } from '$lib/stores/toast';

/** What a send asks the runtime to do with the turn in flight. */
export type ComposerDeliveryIntent = 'apply_now' | 'queue_after_turn';

/**
 * One composer-sent record for both send paths, so a reply sent from a comment thread counts with the
 * same `reply_count` as one typed in the composer.
 */
export function emitComposerSend(threadId: string, content: string, attachmentCount: number, payload: { root_cid?: string }[], deliveryIntent?: ComposerDeliveryIntent): void {
	emitProductEvent('composer', 'sent', {
		thread_id: threadId,
		char_count: content.length,
		attachment_count: attachmentCount,
		comment_count: payload.length,
		reply_count: payload.filter(comment => !!comment.root_cid).length,
		...(deliveryIntent === 'queue_after_turn' ? { delivery_intent: deliveryIntent } : {}),
	});
}

/**
 * Why a reply typed in a comment thread cannot be sent, in the person's words. It sits beside the
 * composer rather than inside it because each known cause gets its own sentence (COPY-STANDARD C11)
 * and each sentence names what happens to the typed reply and what to do next (C9).
 */
export const REPLY_SEND_FAILURE = {
	restarting: 'CORSIA is restarting after an update, so it can’t answer this reply. Your reply stays here; try again once CORSIA is back.',
	noModel: 'This chat’s model can’t answer this reply. Your reply stays here; choose a model, then send it again.',
	notAccepted: 'CORSIA couldn’t send this reply because the connection is unavailable. Your reply stays here; send it again.',
} as const;

export function postChatComment(reply: ChatComment, threadId: string | null | undefined, modelId: string | undefined, restarting: boolean, modelReady: boolean): string {
	// A comment thread exists only on a message in a bound chat, so an unbound composer is never asked to send one.
	if (!threadId) throw new Error('Cannot send reply: no chat');
	const blocked = replySendBlock(restarting, modelReady);
	if (blocked) { toasts.show(REPLY_SEND_FAILURE[blocked], 'error'); throw new Error(`Cannot send reply: ${blocked}`); }
	const { id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid } = reply;
	const payload = [{ id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid }];
	emitComposerSend(threadId, '', 0, payload);
	const inputId = sendMessage(threadId, '', modelId, [], undefined, 'apply_now', payload);
	if (!inputId) {
		toasts.show(REPLY_SEND_FAILURE.notAccepted, 'error');
		throw new Error(REPLY_SEND_FAILURE.notAccepted);
	}
	return inputId;
}

export type ReplySendBlock = keyof typeof REPLY_SEND_FAILURE;

/** The first known reason this reply cannot be sent, or null when it can. Reading it is what picks the sentence. */
export function replySendBlock(restarting: boolean, modelReady: boolean): ReplySendBlock | null {
	if (restarting) return 'restarting';
	if (!modelReady) return 'noModel';
	return null;
}
