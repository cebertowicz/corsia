import type { ChatComment, Message } from '$lib/stores/threads';

/** Hide an empty comment envelope only after every comment is present on a loaded target. */
export function commentsAlreadyPlaced(row: Message, rows: Message[], visibleTargets?: ReadonlySet<string>): boolean {
	const comments = row.metadata?.reply_context?.comments;
	if (row.role !== 'user' || row.content.trim() || !comments?.length || row.metadata?.attachments?.length) return false;
	if (row._optimistic || row.delivery_state === 'queued' || row.delivery_state === 'failed') {
		return comments.every((reply: { root_user_message_id?: string; root_cid?: string }) =>
			!!reply.root_user_message_id && !!reply.root_cid && rows.some(target => target.thread_id === row.thread_id
				&& (!visibleTargets || visibleTargets.has(target.id))
				&& target.chat_comments?.some(root => root.user_message_id === reply.root_user_message_id && root.cid === reply.root_cid)));
	}
	const successors = new Set([row.id]);
	let changed = true;
	while (changed) {
		changed = false;
		for (const candidate of rows) if (candidate.thread_id === row.thread_id && candidate.metadata?.retried_from_message_id
			&& successors.has(candidate.metadata.retried_from_message_id) && !successors.has(candidate.id)) {
			successors.add(candidate.id); changed = true;
		}
	}
	return comments.every((comment: { id?: string }) => !!comment.id && rows.some(candidate => successors.has(candidate.id)
		&& candidate.metadata?.reply_context?.comments?.some((copy: { id?: string; cid?: string }) => copy.id === comment.id && !!copy.cid
			&& rows.some(target => target.thread_id === row.thread_id && (!visibleTargets || visibleTargets.has(target.id))
				&& target.chat_comments?.some(root => (root.user_message_id === candidate.id && root.cid === copy.cid)
					|| root.answers.some(answer => answer.author_kind === 'user' && answer.user_message_id === candidate.id && answer.cid === copy.cid))))));
}

/** The comment a reply send answers, as one key: its root message and comment id. */
const replyKey = (userMessageId?: string, cid?: string) => (userMessageId && cid ? `${userMessageId}:${cid}` : '');

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
 * Every reply send in one thread, keyed by the comment it answers, from a single pass over the loaded rows.
 * Each comment's pending and failed lists used to scan all rows again, which made a transcript render
 * quadratic in its own length; one pass per `$messages` change answers all of them.
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
