import { get, writable } from 'svelte/store';
import { patchSubagentThread, subagentMeta, type SubagentThreadFields } from './subagents';
import { threads } from './threads';

/**
 * The switch and effort fields of a thread the list omits and that is no worker: the scope's
 * conductor or CORSIA agent. It is the row the list would hold if it held them.
 */
export const unlistedThreadRows = writable<Map<string, SubagentThreadFields>>(new Map());

/** Fold fields into an unlisted thread's row; a listed thread or a worker keeps its own row. */
export function patchUnlistedThreadRow(threadId: string, fields: SubagentThreadFields): void {
	if (Object.keys(fields).length === 0) return;
	if (get(threads).some((row) => row.id === threadId) || get(subagentMeta).has(threadId)) return;
	unlistedThreadRows.update((rows) => new Map(rows).set(threadId, { ...rows.get(threadId), ...fields }));
}

/** The one way to change a thread's row fields: the list holds a chat's row, a worker's lives beside the registry, an unlisted thread's beside both. */
export function updateThreadRow(threadId: string, fields: SubagentThreadFields): void {
	threads.update((rows) => rows.map((row) => (row.id === threadId ? { ...row, ...fields } : row)));
	patchSubagentThread(threadId, fields);
	patchUnlistedThreadRow(threadId, fields);
}
