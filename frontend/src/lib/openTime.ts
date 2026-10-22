/**
 * Open time on screen: the server's one number for how long a run or plan step has had
 * something happening in it (a Turn of the agent or any subagent, or an outside wait).
 *
 * Every surface asks here, so the Focus chip, the finished label, the waits chip and a
 * plan row cannot disagree. Asks made in the same tick go out as one request; a failed
 * read is retried a few times before it is logged. The answer counts up locally from
 * the moment it arrived while it says the work is open, and while it stays open the
 * number never goes back: a later open answer that reads lower than what the count had
 * reached waits for the count to catch up. A settled answer is the server's figure.
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
	/** While open, the least this key reads (ms): what the count had reached when this answer arrived. */
	floor: number;
}

export const openTimes = writable<ReadonlyMap<string, OpenTimeReading>>(new Map());

/** The key one thread's range is asked and answered by: the same range on the same thread is one number. */
export function openRangeKey(threadId: string, range: { since: number; until: number | null }): string {
	return `${threadId}|${range.since}|${range.until ?? 'open'}`;
}

/** Under the server's per-request cap. */
const CHUNK = 200;
/** Waits before each retry of a failed read, in ms; past the last the failure is logged and the keys wait for their next ask. */
const RETRY_MS = [1_000, 4_000, 16_000];

let pending = new Map<string, OpenTimeQuery>();
/** Asks made while signed out, sent once a token arrives. */
let unsent = new Map<string, OpenTimeQuery>();
/** Each key's newest request still in flight, so a slow older answer never overwrites a newer one. */
const requestedSeq = new Map<string, number>();
let seq = 0;

token.subscribe((bearer) => {
	if (!bearer || unsent.size === 0) return;
	const queries = [...unsent.values()];
	unsent = new Map();
	requestOpenTime(queries);
});

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
	if (!bearer) {
		for (const query of batch) unsent.set(query.key, query);
		return;
	}
	const chunks = [];
	for (let start = 0; start < batch.length; start += CHUNK) chunks.push(batch.slice(start, start + CHUNK));
	await Promise.all(chunks.map((chunk) => read(bearer, chunk, ++seq, 0)));
}

async function read(bearer: string, chunk: OpenTimeQuery[], mine: number, attempt: number): Promise<void> {
	for (const query of chunk) requestedSeq.set(query.key, mine);
	const answers = await readOpenTime(bearer, chunk);
	const current = chunk.filter((query) => requestedSeq.get(query.key) === mine);
	if (answers === null) {
		if (current.length === 0) return;
		if (attempt >= RETRY_MS.length) {
			for (const query of current) requestedSeq.delete(query.key);
			console.warn(`Open time: ${current.length} reading(s) failed ${attempt + 1} times; they refresh on the next ask`);
			return;
		}
		await new Promise((resolve) => setTimeout(resolve, RETRY_MS[attempt]));
		const still = current.filter((query) => requestedSeq.get(query.key) === mine);
		if (still.length > 0) await read(get(token) ?? bearer, still, mine, attempt + 1);
		return;
	}
	for (const query of current) requestedSeq.delete(query.key);
	const receivedAt = Date.now();
	openTimes.update((readings) => {
		const next = new Map(readings);
		for (const query of current) {
			const answer = answers[query.key];
			if (!answer) continue;
			const before = readings.get(query.key);
			const floor = answer.open_now && before?.openNow ? openMilliseconds(readings, query.key, receivedAt) ?? 0 : 0;
			next.set(query.key, { seconds: answer.seconds, openNow: answer.open_now, receivedAt, floor });
		}
		return next;
	});
}

/**
 * What a key's open time reads at `now` (ms), or null before its first answer.
 * While the work is open the number keeps counting from its answer and never reads
 * below what the count had reached; a settled answer reads as the server gave it.
 */
export function openMilliseconds(
	readings: ReadonlyMap<string, OpenTimeReading>,
	key: string,
	now: number,
): number | null {
	const reading = readings.get(key);
	if (!reading) return null;
	if (!reading.openNow) return reading.seconds * 1000;
	return Math.max(reading.floor, reading.seconds * 1000 + Math.max(0, now - reading.receivedAt));
}

/** Test seam: this memory outlives a mount, so a fresh test starts from empty. */
export function _resetOpenTimeForTests(): void {
	openTimes.set(new Map());
	requestedSeq.clear();
	pending = new Map();
	unsent = new Map();
}
