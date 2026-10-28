import { get, writable } from 'svelte/store';
import { patchSubagentThread, type SubagentThreadFields } from './subagents';
import { threads } from './threads';

/**
 * The switch state of the scope's designated thread (a project's conductor or the CORSIA agent),
 * which the thread list omits. One row, replaced by each read of the scope's target.
 */
export const designatedSwitchRow = writable<{ threadId: string; fields: SubagentThreadFields } | null>(null);

// A pending switch does not move a thread's `updated_at` (only the commit does), so a server time
// cannot order a read against the 202 or frame that changed the row. Each read start and each write
// takes the next tick: a read may replace the row only if nothing wrote to it since the read began.
let tick = 0;
let lastWrite = 0;

/** Mark the start of a read of the scope's target, to hand back to `applyDesignatedSwitchRead`. */
export function beginDesignatedSwitchRead(): number {
	return ++tick;
}

/** Replace the row with a read begun at `startedAt`; a read older than the last write or read is stale. */
export function applyDesignatedSwitchRead(
	startedAt: number,
	row: { threadId: string; fields: SubagentThreadFields } | null,
): void {
	if (startedAt < lastWrite) return;
	lastWrite = startedAt;
	designatedSwitchRow.set(row);
}

/** Fold fields into the designated thread's row; any other thread is ignored. */
export function patchDesignatedSwitchRow(threadId: string, fields: SubagentThreadFields): void {
	const row = get(designatedSwitchRow);
	if (row?.threadId !== threadId || Object.keys(fields).length === 0) return;
	lastWrite = ++tick;
	designatedSwitchRow.set({ threadId, fields: { ...row.fields, ...fields } });
}

/** Forget the row and the clock, for a test. */
export function _resetDesignatedSwitchRowForTests(): void {
	tick = 0;
	lastWrite = 0;
	designatedSwitchRow.set(null);
}

/** The one way to change a thread's row fields: the list holds a chat's row, a worker's lives beside the registry, the designated thread's in its own row. */
export function updateThreadRow(threadId: string, fields: SubagentThreadFields): void {
	threads.update((rows) => rows.map((row) => (row.id === threadId ? { ...row, ...fields } : row)));
	patchSubagentThread(threadId, fields);
	patchDesignatedSwitchRow(threadId, fields);
}
