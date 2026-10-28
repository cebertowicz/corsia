import { get, writable } from 'svelte/store';
import { patchSubagentThread, subagentMeta, type SubagentThreadFields } from './subagents';
import { threads } from './threads';

/**
 * The switch and effort fields of a thread the list omits and that is no worker: the scope's
 * conductor or CORSIA agent. It is the row the list would hold if it held them.
 */
export const unlistedThreadRows = writable<Map<string, SubagentThreadFields>>(new Map());

// A pending switch does not move a thread's `updated_at` (only the commit does), so a server time
// cannot order a read against the 202 that began the switch. Each write and each read start takes
// the next tick instead: a read may replace the row only if nothing wrote to it since the read began.
let tick = 0;
const lastWrite = new Map<string, number>();

function holdsOwnRow(threadId: string): boolean {
	return get(threads).some((row) => row.id === threadId) || get(subagentMeta).has(threadId);
}

function writeUnlistedRow(threadId: string, fields: SubagentThreadFields, at: number): void {
	lastWrite.set(threadId, at);
	unlistedThreadRows.update((rows) => new Map(rows).set(threadId, { ...rows.get(threadId), ...fields }));
}

/** Fold fields into an unlisted thread's row; a listed thread or a worker keeps its own row. */
export function patchUnlistedThreadRow(threadId: string, fields: SubagentThreadFields): void {
	if (Object.keys(fields).length === 0) return;
	if (holdsOwnRow(threadId)) return;
	writeUnlistedRow(threadId, fields, ++tick);
}

/** Mark the start of a server read of an unlisted thread, to hand back to `applyUnlistedThreadRead`. */
export function beginUnlistedThreadRead(): number {
	return ++tick;
}

/**
 * Fold fields from a server read begun at `startedAt` into an unlisted thread's row. A read that
 * began before a later write (a 202, a frame or a newer read) is stale and leaves the row alone.
 */
export function applyUnlistedThreadRead(threadId: string, startedAt: number, fields: SubagentThreadFields): void {
	if (holdsOwnRow(threadId)) return;
	if (startedAt < (lastWrite.get(threadId) ?? 0)) return;
	writeUnlistedRow(threadId, fields, startedAt);
}

/** Forget every write time, for a test that resets the unlisted rows. */
export function _resetUnlistedThreadRowsForTests(): void {
	lastWrite.clear();
	unlistedThreadRows.set(new Map());
}

/** The one way to change a thread's row fields: the list holds a chat's row, a worker's lives beside the registry, an unlisted thread's beside both. */
export function updateThreadRow(threadId: string, fields: SubagentThreadFields): void {
	threads.update((rows) => rows.map((row) => (row.id === threadId ? { ...row, ...fields } : row)));
	patchSubagentThread(threadId, fields);
	patchUnlistedThreadRow(threadId, fields);
}
