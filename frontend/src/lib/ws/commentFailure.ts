import { messages, type Message } from '../stores/threads';
import type { ChatCommentAnchor } from '$lib/chatCommentAnchors';
import { chatReplyDrafts } from '../components/chatReplyDrafts.svelte';
import { emit } from './events';

type QueuedFrame = { thread_id: string; input_id: string; message_id: string; created_at?: string; delivery_state?: string; delivery_error?: string | null; chat_comment_target_id?: string };

/** A comment's input was stored: its draft learns it, and the frame names the message it comments on so that thread refreshes. */
export function settleQueuedComment(frame: QueuedFrame): void {
	// A stored input that failed carries its own failure and Retry in its comment's thread, so that thread opens.
	const failed = !!frame.delivery_state && !['queued', 'delivered'].includes(frame.delivery_state);
	const draft = chatReplyDrafts.settle(frame.thread_id, frame.input_id, failed ? 'stored_failed' : 'queued');
	if (draft) frame.chat_comment_target_id = draft.targetMessageId;
	if (draft && failed) showStoredCommentFailure(frame, draft);
}

type StoredRoot = { threadId: string; targetMessageId: string; text: string; anchor?: ChatCommentAnchor; seq?: number; postedId?: string; commentId?: string };

/**
 * A root comment that was stored and then failed: its thread opens on the failure, with its Retry.
 * The sending socket never receives the stored row, so the row the thread reads its failure from is
 * written here, from the draft that posted it, before the thread is asked to open.
 */
function showStoredCommentFailure(frame: QueuedFrame, draft: StoredRoot): void {
	if (draft.postedId && draft.anchor && typeof draft.seq === 'number') {
		const { postedId: id, anchor, seq } = draft;
		const row: Message = {
			id: frame.message_id, thread_id: frame.thread_id, role: 'user', content: '',
			metadata: { input_id: frame.input_id, reply_context: { comments: [{ id, seq, ...anchor, note: draft.text }] } },
			created_at: frame.created_at ?? new Date().toISOString(),
			delivery_state: 'failed', delivery_error: frame.delivery_error ?? null,
		};
		messages.update(rows => rows.some(existing => existing.id === row.id) ? rows : [...rows, row]);
	}
	emit('chat_comment_draft_error', { threadId: draft.threadId, targetMessageId: draft.targetMessageId, commentId: draft.commentId ?? draft.postedId });
}
