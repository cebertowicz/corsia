import { get, writable } from 'svelte/store';
import { activeThreadId, messages, type Message } from './threads';
import { mergeFetchedHistorySnapshot } from '../messageHistory';
import { hydrateMeasuredFromRows } from './contextUsageByThread';

/**
 * Threads whose transcript is on screen besides the active one — a subagent
 * conversation open in the artifact leaf (#1949).
 *
 * The `messages` store holds rows tagged with their own `thread_id`; every
 * view filters by that id. This set decides only whether an incoming update is
 * applied to the store at all, so a displayed thread gets exactly the same
 * delivery transitions as the active thread, from the same writers.
 */
const claims = writable<ReadonlyMap<string, number>>(new Map());

/** Whether updates for this thread belong in the message store: it is active or a view claims it. */
export function isDisplayedThread(threadId: string | null | undefined): boolean {
	if (!threadId) return false;
	return threadId === get(activeThreadId) || (get(claims).get(threadId) ?? 0) > 0;
}

/**
 * Claim a thread for a mounted view. Returns the release; the last release
 * drops the thread's rows unless the thread is still active or claimed.
 */
export function claimDisplayedThread(threadId: string): () => void {
	claims.update((current) => {
		const next = new Map(current);
		next.set(threadId, (next.get(threadId) ?? 0) + 1);
		return next;
	});
	let released = false;
	return () => {
		if (released) return;
		released = true;
		claims.update((current) => {
			const next = new Map(current);
			const remaining = (next.get(threadId) ?? 1) - 1;
			if (remaining > 0) next.set(threadId, remaining);
			else next.delete(threadId);
			return next;
		});
		if (isDisplayedThread(threadId)) return;
		messages.update((rows) => rows.filter((row) => row.thread_id !== threadId));
	};
}

/**
 * Adopt a fetched first page as `threadId`'s rows, keeping every other
 * displayed thread's rows. The page's newest stored receipt fills the
 * thread's context reading, so the donut is not empty on entry or reload.
 */
export function adoptThreadSnapshot(threadId: string, rows: Message[]): void {
	messages.update((current) => mergeFetchedHistorySnapshot(current, rows, threadId, isDisplayedThread));
	hydrateMeasuredFromRows(threadId, rows);
}

/** Keep `threadId`'s rows and those of any other thread still on screen; drop the rest. */
export function retainDisplayedRows(threadId: string): void {
	messages.update((current) => current.filter((row) =>
		row.thread_id === threadId || isDisplayedThread(row.thread_id)));
}

export function _resetDisplayedThreadsForTests(): void {
	claims.set(new Map());
}
