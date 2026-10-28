import { get, writable } from 'svelte/store';
import type { CommentReplyItem } from '$lib/api/attentionQueue';
import { token } from '$lib/stores/auth';
import { activeThreadId } from '$lib/stores/threads';

export const chatReplyAttention = writable<{ threadId: string; items: CommentReplyItem[] } | null>(null);
// Every endpoint returns a thread-wide snapshot, including message pages.
// Order requests across all callers so a late response cannot restore older state.
let requestSequence = 0;
let appliedSequence = 0;
let identity = get(token);
let selectedThread = get(activeThreadId);
function invalidate(): void {
	appliedSequence = ++requestSequence;
	chatReplyAttention.set(null);
}
token.subscribe(value => {
	if (value !== identity) invalidate();
	identity = value;
});
activeThreadId.subscribe(value => {
	if (value !== selectedThread) invalidate();
	selectedThread = value;
});

export function beginChatReplyAttentionRead(): number {
	return ++requestSequence;
}

export function setChatReplyAttention(threadId: string, items: CommentReplyItem[], sequence: number): void {
	if (get(activeThreadId) !== threadId || sequence < appliedSequence) return;
	appliedSequence = sequence;
	chatReplyAttention.set({ threadId, items });
}

export async function refreshChatReplyAttention(threadId: string): Promise<void> {
	const bearer = get(token);
	if (!bearer || get(activeThreadId) !== threadId) return;
	const sequence = beginChatReplyAttentionRead();
	const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/chat-comment-threads`, {
		method: 'POST', headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ message_ids: [] }),
	});
	if (!response.ok) throw new Error(`HTTP ${response.status}`);
	const data = await response.json();
	if (get(token) === bearer) setChatReplyAttention(threadId, data.chat_reply_items ?? [], sequence);
}
