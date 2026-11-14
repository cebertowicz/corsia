interface ReplacementRow {
	id?: string;
	thread_id?: string;
	replaced_message_ids?: string[];
}
const removed = new Map<string, Set<string>>();

/** Keep removal knowledge for the lifetime of a thread's loaded transcript. */
export function rememberReplacement(thread: string, ids: string[]): void {
	const known = removed.get(thread) ?? new Set<string>();
	for (const id of ids) if (typeof id === 'string') known.add(id);
	removed.set(thread, known);
}

/** Learn page/frame ancestry first, then reject stale rows across every merge. */
export function pruneReplacedRows<T extends ReplacementRow>(rows: T[]): T[] {
	for (const row of rows) if (row.thread_id && Array.isArray(row.replaced_message_ids)) {
		rememberReplacement(row.thread_id, row.replaced_message_ids);
	}
	return rows.filter(row => !row.thread_id || !row.id || !removed.get(row.thread_id)?.has(row.id));
}

const readEpochs = new Map<string, number>();
/** A reconnect invalidates pending transcript reads as well as retained rows. */
export function invalidateTranscriptReads(thread: string): void {
	readEpochs.set(thread, transcriptReadEpoch(thread) + 1);
}
/** Capture before a read and compare before adopting its result. */
export function transcriptReadEpoch(thread: string): number { return readEpochs.get(thread) ?? 0; }

/** Reset session knowledge only in isolated tests. */
export function resetReplacementsForTests(): void { removed.clear(); readEpochs.clear(); }
