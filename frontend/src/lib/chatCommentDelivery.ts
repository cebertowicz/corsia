import type { ChatComment, Message } from '$lib/stores/threads';

/** The comment a reply send answers, as one key: its root message and comment id. */
const replyKey = (userMessageId?: string, cid?: string) => (userMessageId && cid ? `${userMessageId}:${cid}` : '');

/** A comment-only input: a posted comment or reply, with no chat text or attachment of its own. */
export function isCommentEnvelope(row: Message): boolean {
	return row.role === 'user' && !row.content.trim() && !row.metadata?.attachments?.length
		&& !!row.metadata?.reply_context?.comments?.length;
}

/** Comment-only inputs live in their comment threads, regardless of target loading or delivery state; a failed one shows its failure there. */
export function suppressibleCommentEnvelopes(rows: Message[], threadId: string | null | undefined): Set<string> {
	return new Set(rows.filter(row => row.thread_id === threadId && isCommentEnvelope(row)).map(row => row.id));
}

function supersededReplies(rows: Message[], threadId: string | null | undefined): Set<string> {
	const superseded = new Set<string>();
	for (const row of rows) {
		if (row.thread_id !== threadId || !row.metadata?.retried_from_message_id) continue;
		if (row.delivery_state === 'delivered' || row.delivery_state === 'failed') superseded.add(row.metadata.retried_from_message_id);
	}
	return superseded;
}

const addTo = (index: Map<string, Message[]>, key: string, seen: Set<string>, row: Message) => {
	if (seen.has(`${key}|${row.id}`)) return;
	seen.add(`${key}|${row.id}`);
	const list = index.get(key);
	if (list) list.push(row); else index.set(key, [row]);
};

export type ChatReplyIndex = {
	/** Sends still awaiting delivery to this thread of this comment. */
	pending(comment: ChatComment): Message[];
	/** Failed sends of this comment or addressed to its thread, with no later send already replacing them. */
	failed(comment: ChatComment): Message[];
};

/**
 * Every reply send in one thread, keyed by the comment it answers, from a single pass over the loaded rows,
 * so one pass per `$messages` change answers every comment's pending and failed lists.
 */
export function chatReplyIndex(rows: Message[], threadId: string): ChatReplyIndex {
	// A failed send is hidden once a retry of it reaches the server, whether that retry landed or failed in turn.
	const superseded = supersededReplies(rows, threadId);
	const pendingByKey = new Map<string, Message[]>();
	const failedByKey = new Map<string, Message[]>();
	const pendingSeen = new Set<string>();
	const failedSeen = new Set<string>();
	// A root comment's own failed send, keyed by the comment's id: the sender learns of the failure before the server's cid reaches it.
	const failedRoots = new Map<string, Message[]>();
	for (const row of rows) {
		if (row.thread_id !== threadId || row.role !== 'user') continue;
		const targets = row.metadata?.reply_context?.comments;
		if (!Array.isArray(targets)) continue;
		// A send the server has not stored yet shows in the box it was typed in, not in a thread.
		if (row._optimistic) continue;
		const sending = row.delivery_state === 'queued';
		const broken = row.delivery_state === 'failed' && !superseded.has(row.id);
		if (!sending && !broken) continue;
		for (const reply of targets) {
			const key = replyKey(reply.root_user_message_id, reply.root_cid);
			// A root comment that failed after it was stored has its failure and Retry in its own thread.
			if (!key) { if (broken) addTo(failedRoots, replyKey(row.id, reply.id), failedSeen, row); continue; }
			if (sending) addTo(pendingByKey, key, pendingSeen, row);
			if (broken) addTo(failedByKey, key, failedSeen, row);
		}
	}
	const of = (index: Map<string, Message[]>) => (comment: ChatComment) => index.get(replyKey(comment.user_message_id, comment.cid)) ?? [];
	// A comment's own send drops out once its text is recorded on that comment as a user answer; that check reads the comment's answers, not the transcript.
	return {
		pending: comment => of(pendingByKey)(comment).filter(row => !comment.answers?.some(answer => answer.author_kind === 'user' && answer.user_message_id === row.id)),
		failed: comment => [...of(failedByKey)(comment), ...(failedRoots.get(replyKey(comment.user_message_id, comment.id)) ?? [])],
	};
}
