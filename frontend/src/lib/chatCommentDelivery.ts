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

/** Find sends still awaiting delivery to this thread of a placed chat comment. */
export function pendingChatReplies(rows: Message[], threadId: string, comment: ChatComment): Message[] {
	return rows.filter(row => row.thread_id === threadId && row.role === 'user'
		&& !comment.answers?.some(answer => answer.author_kind === 'user' && answer.user_message_id === row.id)
		&& (row._optimistic || row.delivery_state === 'queued')
		&& Array.isArray(row.metadata?.reply_context?.comments)
		&& row.metadata.reply_context.comments.some((reply: { root_user_message_id?: string; root_cid?: string }) =>
			reply.root_user_message_id === comment.user_message_id && reply.root_cid === comment.cid));
}

/** Find failed sends addressed to this thread of a placed chat comment. */
export function failedChatReplies(rows: Message[], threadId: string, comment: ChatComment): Message[] {
	return rows.filter(row => row.thread_id === threadId && row.role === 'user' && row.delivery_state === 'failed'
		&& !rows.some(successor => successor.thread_id === threadId && successor.metadata?.retried_from_message_id === row.id && (successor.delivery_state === 'delivered' || successor.delivery_state === 'failed'))
		&& Array.isArray(row.metadata?.reply_context?.comments)
		&& row.metadata.reply_context.comments.some((reply: { root_user_message_id?: string; root_cid?: string }) =>
			reply.root_user_message_id === comment.user_message_id && reply.root_cid === comment.cid));
}
