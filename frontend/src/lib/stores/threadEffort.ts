import { get } from 'svelte/store';
import { subagentThreadRows } from './subagents';
import { updateThreadRow } from './threadRow';
import { activeThreadId, reasoningEffort, threads } from './threads';

/** A thread's effort wherever it lives: the open thread's global, its list row, or a worker's own row. */
function writeThreadEffort(threadId: string, effort: string | null): void {
	updateThreadRow(threadId, { reasoning_effort: effort });
	if (threadId === get(activeThreadId)) reasoningEffort.set(effort);
}

// Per thread: the newest save, whether it failed, the saves still in flight, and the effort the server last
// confirmed. Once none is in flight, a failed newest save leaves the confirmed effort on the row.
const saves = new Map<string, { latest: number; latestFailed: boolean; inflight: number; confirmed: string | null }>();

/**
 * Save a thread's effort. Answers null when it saved (or a newer pick took its place), else the sentence
 * to show: a refused or failed newest save leaves the last confirmed effort and says so.
 */
export async function saveReasoningEffort(threadId: string, effort: string | null, token: string): Promise<string | null> {
	let state = saves.get(threadId);
	if (!state || state.inflight === 0) {
		const current = threadId === get(activeThreadId)
			? get(reasoningEffort)
			: get(threads).find(s => s.id === threadId)?.reasoning_effort ?? get(subagentThreadRows).get(threadId)?.reasoning_effort ?? null;
		state = { latest: state?.latest ?? 0, latestFailed: false, inflight: 0, confirmed: current };
		saves.set(threadId, state);
	}
	const mine = ++state.latest;
	state.inflight += 1;
	writeThreadEffort(threadId, effort);
	let failure: string | null = null;
	try {
		const res = await fetch(`/api/threads/${threadId}?token=${encodeURIComponent(token)}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ reasoning_effort: effort }),
		});
		if (res.ok) state.confirmed = effort;
		else {
			console.warn(`[effort] PATCH /api/threads/${threadId} -> ${res.status}`);
			failure = "Couldn't save the thinking level, so it's back to what it was. Try again.";
		}
	} catch (err) {
		console.warn('[effort] save failed', err);
		failure = "Couldn't reach the server, so the thinking level is back to what it was. Try again.";
	}
	state.inflight -= 1;
	if (state.latest === mine) state.latestFailed = failure !== null;
	if (state.inflight === 0 && state.latestFailed) writeThreadEffort(threadId, state.confirmed);
	return state.latest === mine ? failure : null;
}
