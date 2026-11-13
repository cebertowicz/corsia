import { get } from 'svelte/store';
import { emitProductEvent } from '$lib/product_events';
import { CommentSendRefused, unknownSendFailure, type ChatComment } from '$lib/chatCommentAnchors';
import { retryRefusedForGood } from '$lib/chatCommentDelivery';
import { adoptCommentThreads } from '$lib/chatCommentRefresh';
import { apiFetch } from '$lib/api/request';
import { findModelById, workspaceModels } from '$lib/stores/models';
import { reasoningEffort, threads } from '$lib/stores/threads';
import { toasts } from '$lib/stores/toast';
import { viewingDocumentForThread } from '$lib/stores/documentViewport';
import { canEditCheckpointWindow } from './v7/modelWarning';

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

/**
 * Whether the person can raise this chat's model's context window in its details, by the model details' own rule
 * read from the catalog row: an external server's model, a harness's (its label is editable), or oMLX's.
 */
function windowEditable(threadId: string): boolean {
	const row = findModelById(get(workspaceModels), get(threads).find(thread => thread.id === threadId)?.model);
	return !!row && canEditCheckpointWindow(!!row.litellm_model?.startsWith('external@'), !!row.runs_on, row.provider ?? '');
}

/**
 * What the comment's box (`sent`) or a Retry's toast (`retried`) says for a refusal the route names. Each known
 * cause has its own line (COPY-STANDARD C11); `final` when no press can get past it, so no Retry is offered.
 * Any other failure is one the person can only retry, so it carries the reference it is logged under (C10).
 * Only the box keeps the text, so only a box line says so.
 */
export function commentFailure(detail: unknown, reference: unknown, noun: 'comment' | 'reply', act: 'sent' | 'retried', threadId: string): { text: string; final: boolean } {
	const kept = act === 'sent' ? ' Your text stays here to copy.' : '';
	if (detail === 'chat_not_found') return { text: `This chat is deleted, so this ${noun} couldn’t be ${act}.${kept}`, final: true };
	if (detail === 'comment_quote_gone') return { text: `The text this ${noun} quotes isn’t in the chat, so this ${noun} couldn’t be ${act}.${kept}`, final: true };
	if (detail === 'comment_model_cap') {
		const next = windowEditable(threadId) ? 'Choose another model or raise its context window in the model’s details, then try again.' : 'Choose another model, then try again.';
		return { text: `This chat’s model has too small a context window to answer, so this ${noun} couldn’t be ${act}. ${next}`, final: false };
	}
	const generic = act === 'sent' ? unknownSendFailure(noun) : `Couldn’t retry this ${noun}. Try again.`;
	return { text: typeof reference === 'string' && reference ? `${generic} Reference: ${reference}` : generic, final: false };
}

/** Read the post's answer: the commented rows' threads as they now stand, or the sentence the box shows. */
async function adoptPosted(response: Response, threadId: string, noun: 'comment' | 'reply', act: 'sent' | 'retried'): Promise<void> {
	const result = await response.json().catch(() => null);
	if (!response.ok) {
		const failure = commentFailure(result?.detail, result?.reference, noun, act, threadId);
		throw new CommentSendRefused(failure.text, failure.final);
	}
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
	await adoptPosted(response, threadId, noun, 'sent');
	unsettledPosts.delete(key);
	// Counted once the server holds it: a refused press, or a press again after a lost answer, is not another comment.
	emitComposerSend(threadId, '', 0, [comment]);
}

/**
 * Retry a comment, or a reply, whose delivery ended undelivered: the server posts its comments again in its
 * place, as one input however often it is pressed. A refusal says why in a toast, in the comment's words; one
 * no press can get past takes that comment's Retry away.
 */
export async function retryChatComment(threadId: string, messageId: string, noun: 'comment' | 'reply'): Promise<void> {
	try {
		const response = await apiFetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comments`, {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ retry_of: messageId }),
		});
		await adoptPosted(response, threadId, noun, 'retried');
	} catch (error) {
		const refused = error instanceof CommentSendRefused;
		if (refused && error.final) retryRefusedForGood(messageId);
		toasts.show(refused ? error.message : `Couldn’t retry this ${noun}. Try again.`, 'error');
	}
}

export type ReplySendBlock = 'restarting' | 'noModel' | 'notAccepted';

/** The first known reason this reply cannot be sent, or null when it can. Reading it is what picks the sentence. */
export function replySendBlock(restarting: boolean, modelReady: boolean): ReplySendBlock | null {
	if (restarting) return 'restarting';
	if (!modelReady) return 'noModel';
	return null;
}
