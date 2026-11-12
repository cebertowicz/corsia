import type { ChatComment } from '$lib/stores/threads';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';

/** A comment, or a reply in its thread, that the server stored but could not deliver to the agent. */
export type FailedSend = { messageId: string; note: string; createdAt: string; root: boolean };

/**
 * What of this comment's thread was not delivered, read from the server's delivery state on the comment and
 * on each reply the person wrote in it. Each keeps its text and Retry in the thread. A retry replaces the
 * failed comment on the server, so a failure it superseded is no longer on the comment.
 */
export function failedSends(comment: ChatComment): FailedSend[] {
	const failed: FailedSend[] = comment.delivery_state === 'failed'
		? [{ messageId: comment.user_message_id, note: comment.note, createdAt: comment.created_at, root: true }] : [];
	for (const answer of comment.answers) {
		if (answer.author_kind === 'user' && answer.delivery_state === 'failed') failed.push({ messageId: answer.user_message_id, note: answer.text, createdAt: answer.created_at, root: false });
	}
	return failed;
}

/** A reply typed in this comment's thread is on its way to the server. */
export function replySending(comment: ChatComment): boolean {
	return !!chatCommentDrafts.reply(comment.id)?.posting;
}
