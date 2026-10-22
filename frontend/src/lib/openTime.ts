/**
 * Open time on screen: the server's one number for how long a run or plan step has had
 * something happening in it (a Turn of the agent or any subagent, or an outside wait).
 *
 * Every surface asks here, so the Focus chip, the finished label, the waits chip and a
 * plan row cannot disagree. Asks made in the same tick go out as one request. The
 * answer counts up locally from the moment it arrived while it says the work is open,
 * and the shown number never goes back for a key: a later answer that reads lower than
 * what the screen already showed waits for the count to catch up.
 */
import { get, writable } from 'svelte/store';
import { readOpenTime, type OpenTimeQuery } from '$lib/api/openTime';
import { token } from '$lib/stores/auth';

export type { OpenTimeQuery } from '$lib/api/openTime';

export interface OpenTimeReading {
	seconds: number;
	openNow: boolean;
	/** The client clock (ms) when the answer arrived: the count-up runs from here, free of clock skew. */
	receivedAt: number;
}

export const openTimes = writable<ReadonlyMap<string, OpenTimeReading>>(new Map());

/** The key one thread's range is asked and answered by: the same range on the same thread is one number. */
export function openRangeKey(threadId: string, range: { since: number; until: number | null }): string {
	return `${threadId}|${range.since}|${range.until ?? 'open'}`;
}

/** The most each key has shown, in ms. */
const shown = new Map<string, number>();

/** Under the server's per-request cap. */
const CHUNK = 200;

let pending = new Map<string, OpenTimeQuery>();
/** Each key's newest request, so a slow older answer never overwrites a newer one. */
const requestedSeq = new Map<string, number>();
let seq = 0;

/** Ask for these keys' open time; same-tick asks share one request. A key asked again is re-read. */
export function requestOpenTime(queries: readonly OpenTimeQuery[]): void {
	if (queries.length === 0) return;
	if (pending.size === 0) queueMicrotask(flush);
	for (const query of queries) pending.set(query.key, query);
}

async function flush(): Promise<void> {
	const batch = [...pending.values()];
	pending = new Map();
	const bearer = get(token);
	if (!bearer) return;
	for (let start = 0; start < batch.length; start += CHUNK) {
		const chunk = batch.slice(start, start + CHUNK);
		const mine = ++seq;
		for (const query of chunk) requestedSeq.set(query.key, mine);
		const answers = await readOpenTime(bearer, chunk);
		if (answers === null) continue;
		const receivedAt = Date.now();
		openTimes.update((current) => {
			const next = new Map(current);
			for (const query of chunk) {
				const answer = answers[query.key];
				if (!answer || requestedSeq.get(query.key) !== mine) continue;
				next.set(query.key, { seconds: answer.seconds, openNow: answer.open_now, receivedAt });
			}
			return next;
		});
	}
}

/**
 * What a key's open time reads at `now` (ms), or null before its first answer.
 * While the work is open the number keeps counting from its answer; it never reads
 * below what it already showed for this key.
 */
export function openMilliseconds(
	readings: ReadonlyMap<string, OpenTimeReading>,
	key: string,
	now: number,
): number | null {
	const reading = readings.get(key);
	if (!reading) return null;
	const counted = reading.seconds * 1000 + (reading.openNow ? Math.max(0, now - reading.receivedAt) : 0);
	const high = Math.max(shown.get(key) ?? 0, counted);
	shown.set(key, high);
	return high;
}

/** Test seam: this memory outlives a mount, so a fresh test starts from empty. */
export function _resetOpenTimeForTests(): void {
	openTimes.set(new Map());
	shown.clear();
	requestedSeq.clear();
	pending = new Map();
}
