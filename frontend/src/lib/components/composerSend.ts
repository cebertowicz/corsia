import { get } from 'svelte/store';
import { emitProductEvent } from '$lib/product_events';
import { CommentSendRefused, unknownSendFailure, type ChatComment } from '$lib/chatCommentAnchors';
import { adoptCommentThreads } from '$lib/chatCommentRefresh';
import { apiFetch } from '$lib/api/request';
import { plainErrorText } from '$lib/plainError';
import { reasoningEffort } from '$lib/stores/threads';
import { toasts } from '$lib/stores/toast';
import { viewingDocumentForThread } from '$lib/stores/documentViewport';

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

/**
 * The request a press of a box sent, by what it posts, until the server stores it. Pressing again after a
 * refusal or a lost response sends that same request, so the server sees one input, never a second copy.
 */
const unsettledPosts = new Map<string, string>();

/** Read the post's answer: the commented rows' threads as they now stand, or the sentence the box shows. */
async function adoptPosted(response: Response, threadId: string, noun: 'comment' | 'reply'): Promise<void> {
	const result = await response.json().catch(() => null);
	// No press can send it now, so the box offers no Retry (final); the text stays for the person to copy.
	if (!response.ok && result?.detail === 'chat_not_found') throw new CommentSendRefused(`This chat is deleted, so this ${noun} couldn’t be sent. Your text stays here to copy.`, true);
	if (!response.ok) throw new CommentSendRefused(plainErrorText(result?.detail, unknownSendFailure(noun)));
	adoptCommentThreads(threadId, Array.isArray(result?.messages) ? result.messages : []);
}

/**
 * Post a comment, or a reply in a comment thread, from its own box. It is the chat's next input for the
 * agent but no chat message: the server keeps it on the comment it belongs to. Resolves once the server
 * has stored it, with the commented rows' threads as they now stand on screen; rejects with the sentence
 * the box shows when it was not stored.
 */
export async function postChatComment(reply: ChatComment, threadId: string | null | undefined, restarting: boolean, modelReady: boolean): Promise<void> {
	// A comment thread exists only on a message in a bound chat, so an unbound composer is never asked to send one.
	if (!threadId) throw new Error('Cannot send reply: no chat');
	const noun = reply.root_cid ? 'reply' : 'comment';
	const blocked = replySendBlock(restarting, modelReady);
	if (blocked) refuse(blocked, noun);
	const { id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid } = reply;
	const comment = { id, seq, from_off, to_off, quote, anchor_prefix, anchor_suffix, note, root_user_message_id, root_cid };
	// Each press makes the comment a new id, so the post is known by what it says and where.
	const key = JSON.stringify([threadId, seq, from_off, to_off, quote, note, root_user_message_id ?? null, root_cid ?? null]);
	let body = unsettledPosts.get(key);
	if (!body) {
		const viewingDocument = viewingDocumentForThread(threadId);
		const effort = get(reasoningEffort);
		body = JSON.stringify({ input_id: crypto.randomUUID(), comment, ...(viewingDocument ? { viewing_document: viewingDocument } : {}), ...(effort ? { reasoning_effort: effort } : {}) });
		unsettledPosts.set(key, body);
	}
	let response: Response;
	try {
		response = await apiFetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comments`, {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
		});
	} catch { refuse('notAccepted', noun); }
	await adoptPosted(response, threadId, noun);
	unsettledPosts.delete(key);
	// Counted once the server holds it: a refused press, or a press again after a lost answer, is not another comment.
	emitComposerSend(threadId, '', 0, [comment]);
}

/**
 * Retry a comment, or a reply, whose delivery ended undelivered: the server posts its comments again in its
 * place, as one input however often it is pressed. A refusal says so where chat says it for a message.
 */
export async function retryChatComment(threadId: string, messageId: string, noun: 'comment' | 'reply'): Promise<void> {
	const failure = `Couldn’t retry this ${noun}. Try again.`;
	try {
		const response = await apiFetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comments`, {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ retry_of: messageId }),
		});
		await adoptPosted(response, threadId, noun);
	} catch (error) {
		toasts.show(error instanceof CommentSendRefused && error.message !== unknownSendFailure(noun) ? error.message : failure, 'error');
	}
}

export type ReplySendBlock = 'restarting' | 'noModel' | 'notAccepted';

/** The first known reason this reply cannot be sent, or null when it can. Reading it is what picks the sentence. */
export function replySendBlock(restarting: boolean, modelReady: boolean): ReplySendBlock | null {
	if (restarting) return 'restarting';
	if (!modelReady) return 'noModel';
	return null;
}
