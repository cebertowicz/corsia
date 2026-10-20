import { get } from 'svelte/store';
import { subagentThreadRows } from './subagents';
import { updateThreadRow } from './threadRow';
import { activeThreadId, reasoningEffort, threads } from './threads';

/** A thread's effort wherever it lives: the open thread's global, its list row, or a worker's own row. */
function writeThreadEffort(threadId: string, effort: string | null): void {
	updateThreadRow(threadId, { reasoning_effort: effort });
	if (threadId === get(activeThreadId)) reasoningEffort.set(effort);
}

/** Save a thread's effort; a refused or failed save puts the previous effort back and answers false. */
export async function saveReasoningEffort(threadId: string, effort: string | null, token: string): Promise<boolean> {
	const prev = threadId === get(activeThreadId)
		? get(reasoningEffort)
		: get(threads).find(s => s.id === threadId)?.reasoning_effort ?? get(subagentThreadRows).get(threadId)?.reasoning_effort ?? null;
	writeThreadEffort(threadId, effort);
	try {
		const res = await fetch(`/api/threads/${threadId}?token=${token}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ reasoning_effort: effort }),
		});
		if (!res.ok) throw new Error(`PATCH /api/threads/${threadId} -> ${res.status}`);
		return true;
	} catch {
		writeThreadEffort(threadId, prev);
		return false;
	}
}
