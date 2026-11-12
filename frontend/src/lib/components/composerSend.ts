import { emitProductEvent } from '$lib/product_events';
import { CommentSendRefused, type ChatComment } from '$lib/chatCommentAnchors';
import { sendMessage } from '$lib/ws';

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
 * Why a comment or a reply typed in a comment thread cannot be sent, in the person's words. Each known
 * cause gets its own sentence (COPY-STANDARD C11), and each names what happens to the typed text and
 * what to do next (C9). It shows only in the box the text was typed in, which is open whenever a send starts.
 */
function blockedSendText(cause: ReplySendBlock, noun: 'comment' | 'reply'): string {
	if (cause === 'restarting') return `Couldn’t send this ${noun} because CORSIA is restarting after an update. Your ${noun} stays here; try again once CORSIA is back.`;
	if (cause === 'noModel') return `Couldn’t send this ${noun} because this chat’s model can’t answer it. Your ${noun} stays here; choose a model, then try again.`;
	return `Couldn’t send this ${noun} because the connection is unavailable. Your ${noun} stays here; try again.`;
}

function refuse(cause: ReplySendBlock, noun: 'comment' | 'reply'): never {
	throw new CommentSendRefused(blockedSendText(cause, noun));
}

export function postChatComment(reply: ChatComment, threadId: string | null | undefined, modelId: string | undefined, restarting: boolean, modelReady: boolean): string {
	// A comment thread exists only on a message in a bound chat, so an unbound composer is never asked to send one.
	if (!threadId) throw new Error('Cannot send reply: no chat');
	const noun = reply.root_cid ? 'reply' : 'comment';
	const blocked = replySendBlock(restarting, modelReady);
	if (blocked) refuse(blocked, noun);
	const { id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid } = reply;
	const payload = [{ id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid }];
	emitComposerSend(threadId, '', 0, payload);
	const inputId = sendMessage(threadId, '', modelId, [], undefined, 'apply_now', payload);
	if (!inputId) refuse('notAccepted', noun);
	return inputId;
}

export type ReplySendBlock = 'restarting' | 'noModel' | 'notAccepted';

/** The first known reason this reply cannot be sent, or null when it can. Reading it is what picks the sentence. */
export function replySendBlock(restarting: boolean, modelReady: boolean): ReplySendBlock | null {
	if (restarting) return 'restarting';
	if (!modelReady) return 'noModel';
	return null;
}
