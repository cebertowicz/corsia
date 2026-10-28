import { get, writable } from 'svelte/store';
import type { CommentReplyItem } from '$lib/api/attentionQueue';
import { token } from '$lib/stores/auth';
import { activeThreadId } from '$lib/stores/threads';

export const chatReplyAttention = writable<{ threadId: string; items: CommentReplyItem[] } | null>(null);
let identity = get(token);
token.subscribe(value => {
	if (value !== identity) chatReplyAttention.set(null);
	identity = value;
});

export function setChatReplyAttention(threadId: string, items: CommentReplyItem[]): void {
	if (get(activeThreadId) === threadId) chatReplyAttention.set({ threadId, items });
}

export function mergeChatReplyAttention(threadId: string, items: CommentReplyItem[]): void {
	if (get(activeThreadId) !== threadId || !items.length) return;
	chatReplyAttention.update(current => {
		const existing = current?.threadId === threadId ? current.items : [];
		const known = new Set(existing.map(item => item.detail.destination.id));
		return { threadId, items: [...existing, ...items.filter(item => !known.has(item.detail.destination.id))] };
	});
}

export async function refreshChatReplyAttention(threadId: string): Promise<void> {
	const bearer = get(token);
	if (!bearer || get(activeThreadId) !== threadId) return;
	const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comment-threads`, {
		method: 'POST', headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ message_ids: [] }),
	});
	if (!response.ok) throw new Error(`HTTP ${response.status}`);
	const data = await response.json();
	if (get(token) === bearer) setChatReplyAttention(threadId, data.chat_reply_items ?? []);
}
