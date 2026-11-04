import { messages, type Message } from '../stores/threads';

// Persisted error kinds shown as system notices.
const PERSISTED_ERROR_TYPES = new Set(['llm_error', 'cc_error', 'tool_boundary_violation', 'tool_bridge_unavailable']);

/** The Turn ids that already have a saved (sequenced) error row in `rows`. */
function persistedErrorTurns(rows: Message[]): Map<string, Set<string>> {
	const turns = new Map<string, Set<string>>();
	for (const row of rows) {
		const turnId = row.metadata?.turn_id;
		if (
			row.role === 'system' && typeof row.seq === 'number' && typeof turnId === 'string'
			&& PERSISTED_ERROR_TYPES.has(String(row.metadata?.type ?? ''))
		) {
			const threadTurns = turns.get(row.thread_id) ?? new Set<string>();
			threadTurns.add(turnId);
			turns.set(row.thread_id, threadTurns);
		}
	}
	return turns;
}

/** Client rows removed because their saved row arrived: that swap plays no exit (the saved row takes the place). */
export const settledClientErrors = new WeakSet<object>();

/** Drop each client-only error row whose Turn's saved error row is present. Order of arrival does not matter. */
export function settleClientErrors<T extends Message>(rows: T[]): T[] {
	if (!rows.some((row) => row._clientErrorTurnId)) return rows;
	const saved = persistedErrorTurns(rows);
	return rows.filter((row) => {
		const settled = !!row._clientErrorTurnId && saved.get(row.thread_id)?.has(row._clientErrorTurnId) === true;
		if (settled) settledClientErrors.add(row);
		return !settled;
	});
}

/** R1: a Turn other than the one that failed is now running on the thread, so its failed Turn's client row leaves. */
export function dropClientErrorsBefore(threadId: string, runningTurnId: string | null): void {
	messages.update((rows) => rows.some((row) => row.thread_id === threadId && row._clientErrorTurnId && row._clientErrorTurnId !== runningTurnId)
		? rows.filter((row) => !(row.thread_id === threadId && row._clientErrorTurnId && row._clientErrorTurnId !== runningTurnId))
		: rows);
}
