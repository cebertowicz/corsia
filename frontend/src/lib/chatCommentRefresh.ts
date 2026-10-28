import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';
import { reload } from '$lib/stores/attentionDock.svelte';
import { on } from '$lib/ws';

/** Refresh loaded targets when a committed chat comment or answer arrives. */
export function watchChatCommentThreads(pageSize: number): () => void {
	let generation = 0;
	async function refresh(frame: any) {
		const threadId = get(activeThreadId);
		const bearer = get(token);
		if (!threadId || frame?.thread_id !== threadId || !bearer) return;
		const current = ++generation;
		const ids = get(messages).filter(message => message.thread_id === threadId && message.role === 'assistant').map(message => message.id);
		try {
			const byId = new Map<string, any>();
			for (let offset = 0; offset < ids.length; offset += pageSize) {
				const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comment-threads`, {
					method: 'POST', headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json' },
					body: JSON.stringify({ message_ids: ids.slice(offset, offset + pageSize) }),
				});
				if (!response.ok) throw new Error(`HTTP ${response.status}`);
				const data = await response.json();
				if (current !== generation || get(activeThreadId) !== threadId) return;
				for (const message of data.messages) byId.set(message.id, message);
			}
			messages.update(rows => rows.map(row => byId.has(row.id) && row.thread_id === threadId
				? { ...row, chat_comments: byId.get(row.id).chat_comments ?? [], chat_visible_content: byId.get(row.id).chat_visible_content } : row));
			await reload();
		} catch (error) { console.error('Could not refresh chat comment threads', error); }
	}
	const stops = [on('turn_complete', refresh), on('input_queued', refresh), on('input_accepted', refresh),
		on('message_added', frame => {
			if (frame?.message?.metadata?.reply_context?.comments?.length || frame?.message?.content?.includes('[c')) void refresh(frame);
		})];
	return () => { generation++; stops.forEach(stop => stop()); };
}
