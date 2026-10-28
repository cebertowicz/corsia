import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages, type Message } from '$lib/stores/threads';
import { beginChatReplyAttentionRead, setChatReplyAttention } from '$lib/stores/chatReplyAttention';
import type { CommentReplyItem } from '$lib/api/attentionQueue';
import { on } from '$lib/ws';

/** Refresh only loaded targets affected by newly committed comments or answers. */
export function watchChatCommentThreads(pageSize: number): () => void {
	let generation = 0;
	async function read(threadId: string, bearer: string, ids: string[]): Promise<{ rows: Message[]; replies: CommentReplyItem[] | null }> {
		const result: Message[] = [];
		let replies: CommentReplyItem[] | null = null;
		for (let offset = 0; offset < ids.length; offset += pageSize) {
			const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comment-threads`, {
				method: 'POST', headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json' },
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
		const targets = rows.filter(row => row.role === 'assistant' && (row.chat_comments?.length || comments.some(c => c.seq === row.seq)));
		const hasComments = targets.length > 0 || rows.some(row => row.metadata?.reply_context?.comments?.length);
		if (!hasComments) return;
		const current = ++generation;
		const sequence = beginChatReplyAttentionRead();
		try {
			// The server identifies placed answers; do not parse tags in the client.
			const stepIds = Array.isArray(frame.step_ids) ? frame.step_ids : frame.context_message_id ? [frame.context_message_id] : [];
			const lastAssistant = [...rows].reverse().find(row => row.role === 'assistant');
			const answerIds = comments.length ? [] : [...new Set<string>(stepIds.length ? stepIds : lastAssistant ? [lastAssistant.id] : [])];
			const answers = await read(threadId, bearer, answerIds);
			const cids = new Set(answers.rows.flatMap(row => row.chat_answer_cids ?? []));
			const contextComments = rows.flatMap(row => row.metadata?.reply_context?.comments ?? []);
			const changedTargets = rows.filter(row => row.role === 'assistant' && (comments.some(c => c.seq === row.seq) || contextComments.some(c => c.seq === row.seq && cids.has(c.cid)) || row.chat_comments?.some(c => cids.has(c.cid))));
			const targetRows = await read(threadId, bearer, changedTargets.map(row => row.id));
			const refreshed = [...answers.rows, ...targetRows.rows];
			if (current !== generation || get(activeThreadId) !== threadId) return;
			const byId = new Map(refreshed.map(row => [row.id, row]));
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
	})];
	return () => { generation++; stops.forEach(stop => stop()); };
}
