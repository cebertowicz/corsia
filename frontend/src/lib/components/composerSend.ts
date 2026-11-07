import { emitProductEvent } from '$lib/product_events';

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
	noChat: 'This reply has no chat to go to yet. Your reply stays here; send a message in this chat first, then reply again.',
	restarting: 'CORSIA is restarting after an update, so it can’t answer this reply. Your reply stays here; try again once CORSIA is back.',
	noModel: 'No model is ready to answer this reply. Your reply stays here; choose a model, then reply again.',
	notAccepted: 'CORSIA couldn’t send this reply because the connection closed before it arrived. Your reply stays here; send it again.',
} as const;

export type ReplySendBlock = keyof typeof REPLY_SEND_FAILURE;

/** The first known reason this reply cannot be sent, or null when it can. Reading it is what picks the sentence. */
export function replySendBlock(bound: boolean, restarting: boolean, modelReady: boolean): ReplySendBlock | null {
	if (!bound) return 'noChat';
	if (restarting) return 'restarting';
	if (!modelReady) return 'noModel';
	return null;
}
