import { writable } from 'svelte/store';
import { setThreadContent, setThreadReasoning } from './threads';

/** How many times a reply has left the live slot to stand as a transcript row, streamed or not. */
export const replyCommitsByThread = writable<Map<string, number>>(new Map());

/**
 * A step's reasoning and reply have become a transcript row: empty the live slot of them and, when there was a
 * reply, count it, in the same synchronous turn, so the slot sees the row arrive and its content leave as one change.
 */
export function commitLiveStep(threadId: string, content: unknown): void {
	setThreadReasoning(threadId, '');
	setThreadContent(threadId, '');
	if (typeof content !== 'string' || content.length === 0) return;
	replyCommitsByThread.update(m => new Map(m).set(threadId, (m.get(threadId) || 0) + 1));
}
