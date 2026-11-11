import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages, type Message } from '$lib/stores/threads';
import { beginChatReplyAttentionRead, setChatReplyAttention } from '$lib/stores/chatReplyAttention';
import type { CommentReplyItem } from '$lib/api/attentionQueue';
import { on } from '$lib/ws';
import { apiFetch } from '$lib/api/request';

/** Ask the server what landed on a comment, then refresh only the rows it names as touched. */
export function watchChatCommentThreads(pageSize: number, announce: (quote: string) => void = () => {}): () => void {
	let generation = 0;
	async function read(threadId: string, bearer: string, ids: string[]): Promise<{ rows: Message[]; replies: CommentReplyItem[] | null }> {
		const result: Message[] = [];
		let replies: CommentReplyItem[] | null = null;
		for (let offset = 0; offset < ids.length; offset += pageSize) {
			const response = await apiFetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comment-threads`, {
				method: 'POST', headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ message_ids: ids.slice(offset, offset + pageSize) }),
			});
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			const data = await response.json();
			result.push(...data.messages);
			replies = data.chat_reply_items ?? [];
		}
		return { rows: result, replies };
	}
	async function refresh(frame: any, comments: any[] = []) {
		const threadId = get(activeThreadId);
		const bearer = get(token);
		if (!threadId || frame?.thread_id !== threadId || !bearer) return;
		const rows = get(messages).filter(row => row.thread_id === threadId);
		// The server identifies placed answers; do not parse tags in the client.
		const stepIds = Array.isArray(frame.step_ids) ? frame.step_ids : frame.context_message_id ? [frame.context_message_id] : [];
		const lastAssistant = [...rows].reverse().find(row => row.role === 'assistant');
		const answerIds = comments.length ? [] : [...new Set<string>(stepIds.length ? stepIds : lastAssistant ? [lastAssistant.id] : [])];
		// The loaded page cannot answer this. This socket never receives the added-message
		// frame for its own send, and the commented row may not be loaded on this page at all,
		// so only the server's own answer about the turn's rows proves a comment was answered.
		if (!answerIds.length && !comments.length) return;
		const current = ++generation;
		const sequence = beginChatReplyAttentionRead();
		try {
			const answers = await read(threadId, bearer, answerIds);
			const cids = new Set(answers.rows.flatMap(row => row.chat_answer_cids ?? []));
			const contextComments = rows.flatMap(row => row.metadata?.reply_context?.comments ?? []);
			const knownTargets = rows.filter(row => row.role === 'assistant' && (comments.some(c => c.seq === row.seq) || contextComments.some(c => c.seq === row.seq && cids.has(c.cid)) || row.chat_comments?.some(c => cids.has(c.cid) || c.answers.some(answer => answer.cid && cids.has(answer.cid)))));
			// The server names the message a landed reply sits on. A comment this page never carried is
			// still a target: its row is what draws the notice and carries the quote to announce.
			const namedTargets = (answers.replies ?? []).map(item => item.detail.destination.id);
			const targetIds = [...new Set([...knownTargets.map(row => row.id), ...namedTargets])];
			const targetRows = await read(threadId, bearer, targetIds);
			const refreshed = [...answers.rows, ...targetRows.rows];
			if (current !== generation || get(activeThreadId) !== threadId) return;
			const byId = new Map(refreshed.map(row => [row.id, row]));
			if (!comments.length) {
				const existing = new Set(rows.flatMap(row =>
					(row.chat_comments ?? []).flatMap(comment => comment.answers.filter(answer => answer.author_kind === 'agent').map(answer => `${comment.id}:${answer.assistant_message_id}:${answer.cid}`))));
				for (const row of targetRows.rows) for (const comment of row.chat_comments ?? []) for (const answer of comment.answers) {
					if (answer.author_kind !== 'agent' || !answer.unread) continue;
					const key = `${comment.id}:${answer.assistant_message_id}:${answer.cid}`;
					if (!existing.has(key)) { existing.add(key); announce(comment.quote); }
				}
			}
			messages.update(currentRows => currentRows.map(row => {
				const fresh = row.thread_id === threadId ? byId.get(row.id) : undefined;
				return fresh ? { ...row, chat_comments: fresh.chat_comments ?? [], chat_visible_content: fresh.chat_visible_content, chat_answer_cids: fresh.chat_answer_cids } : row;
			}));
			const replies = targetRows.replies ?? answers.replies;
			if (replies !== null) setChatReplyAttention(threadId, replies, sequence);
		} catch (error) { console.error('Could not refresh chat comment threads', error); }
	}
	const stops = [on('turn_complete', frame => refresh(frame)), on('message_added', frame => {
		const comments = frame?.message?.metadata?.reply_context?.comments;
		if (Array.isArray(comments) && comments.length) void refresh(frame, comments);
		// An update can answer comments while its turn keeps running; show them in their threads now.
		else if (frame?.message?.chat_answer_cids?.length) void refresh({ thread_id: frame.thread_id, step_ids: [frame.message.id] });
	})];
	return () => { generation++; stops.forEach(stop => stop()); };
}
