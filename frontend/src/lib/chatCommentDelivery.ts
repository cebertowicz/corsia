import type { ChatComment, Message } from '$lib/stores/threads';

/** The comment a reply send answers, as one key: its root message and comment id. */
const replyKey = (userMessageId?: string, cid?: string) => (userMessageId && cid ? `${userMessageId}:${cid}` : '');

/**
 * The empty comment envelopes in one thread that need no row of their own, because every comment they carry
 * is already shown on a loaded (and, when given, visible) target. One pass builds the keys; each envelope then
 * checks its comments against them, so the transcript asks once per change instead of rescanning per row.
 */
export function placedCommentEnvelopes(rows: Message[], threadId: string | null | undefined, visibleTargets?: ReadonlySet<string>): Set<string> {
	const byId = new Map<string, Message>();
	const retries = new Map<string, string[]>();
	// A send still on its way is placed once its comment's thread is shown; a delivered one once its own copy is.
	const roots = new Set<string>();
	const shown = new Set<string>();
	for (const row of rows) {
		if (row.thread_id !== threadId) continue;
		byId.set(row.id, row);
		const from = row.metadata?.retried_from_message_id;
		if (from) retries.set(from, [...(retries.get(from) ?? []), row.id]);
		if (visibleTargets && !visibleTargets.has(row.id)) continue;
		for (const root of row.chat_comments ?? []) {
			const key = replyKey(root.user_message_id, root.cid);
			if (key) { roots.add(key); shown.add(key); }
			for (const answer of root.answers ?? []) {
				const answerKey = answer.author_kind === 'user' ? replyKey(answer.user_message_id, answer.cid) : '';
				if (answerKey) shown.add(answerKey);
			}
		}
	}
	const placed = new Set<string>();
	for (const row of byId.values()) {
		const comments = row.metadata?.reply_context?.comments;
		if (row.role !== 'user' || row.content.trim() || !comments?.length || row.metadata?.attachments?.length) continue;
		if (row._optimistic || row.delivery_state === 'queued' || row.delivery_state === 'failed') {
			if (comments.every((reply: { root_user_message_id?: string; root_cid?: string }) => roots.has(replyKey(reply.root_user_message_id, reply.root_cid)))) placed.add(row.id);
			continue;
		}
		// A delivered envelope counts as placed through any retry that replaced it.
		const successors = [row.id];
		for (let index = 0; index < successors.length; index++) {
			for (const next of retries.get(successors[index]) ?? []) if (!successors.includes(next)) successors.push(next);
		}
		if (comments.every((comment: { id?: string }) => !!comment.id && successors.some(id => byId.get(id)?.metadata?.reply_context?.comments
			?.some((copy: { id?: string; cid?: string }) => copy.id === comment.id && shown.has(replyKey(id, copy.cid)))))) placed.add(row.id);
	}
	return placed;
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
	/** Failed sends addressed to this thread of this comment, with no later send already replacing them. */
	failed(comment: ChatComment): Message[];
};

/**
 * Every reply send in one thread, keyed by the comment it answers, from a single pass over the loaded rows,
 * so one pass per `$messages` change answers every comment's pending and failed lists.
 */
export function chatReplyIndex(rows: Message[], threadId: string): ChatReplyIndex {
	// A failed send is hidden once a retry of it reaches the server, whether that retry landed or failed in turn.
	const superseded = new Set<string>();
	for (const row of rows) {
		if (row.thread_id !== threadId || !row.metadata?.retried_from_message_id) continue;
		if (row.delivery_state === 'delivered' || row.delivery_state === 'failed') superseded.add(row.metadata.retried_from_message_id);
	}
	const pendingByKey = new Map<string, Message[]>();
	const failedByKey = new Map<string, Message[]>();
	const pendingSeen = new Set<string>();
	const failedSeen = new Set<string>();
	for (const row of rows) {
		if (row.thread_id !== threadId || row.role !== 'user') continue;
		const targets = row.metadata?.reply_context?.comments;
		if (!Array.isArray(targets)) continue;
		const sending = row._optimistic || row.delivery_state === 'queued';
		const broken = row.delivery_state === 'failed' && !superseded.has(row.id);
		if (!sending && !broken) continue;
		for (const reply of targets) {
			const key = replyKey(reply.root_user_message_id, reply.root_cid);
			if (!key) continue;
			if (sending) addTo(pendingByKey, key, pendingSeen, row);
			if (broken) addTo(failedByKey, key, failedSeen, row);
		}
	}
	const of = (index: Map<string, Message[]>) => (comment: ChatComment) => index.get(replyKey(comment.user_message_id, comment.cid)) ?? [];
	// A comment's own send drops out once its text is recorded on that comment as a user answer; that check reads the comment's answers, not the transcript.
	return {
		pending: comment => of(pendingByKey)(comment).filter(row => !comment.answers?.some(answer => answer.author_kind === 'user' && answer.user_message_id === row.id)),
		failed: of(failedByKey),
	};
}
