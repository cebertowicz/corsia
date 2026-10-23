/**
 * Run stubs: what a Focus page carries in place of a collapsed run's rows.
 *
 * A stub names the run's oldest and newest sequenced display rows (both
 * inclusive), counts them, and sums their activity weight, so the run can be
 * labelled and placed without its rows. Expanding the run reads that range.
 * Stubs carry no key: the transcript derives each fold's expansion tokens from
 * the stub and the rows it holds, exactly as it does for a loaded run.
 */

import { parseBrowserChip, type BrowserChip } from './browserChip';

export interface RunStub {
	firstId: string;
	lastId: string;
	firstSeq: number;
	lastSeq: number;
	/** Display rows in the run, as many as expanding it renders. */
	count: number;
	/** Distinct non-null Turn ids, oldest first. */
	turnIds: string[];
	/** Summed server activity weight of the run's rows. */
	weight: number;
	/** Distinct row kinds the run holds, sorted (`RowKind`s the server counted; never row text). */
	kinds: string[];
	/** Older rows of this run may sit on a page not read yet: the server cut the run at its scan edge. */
	continuesOlder: boolean;
	/** The window the run's newest open put in front of the person, as its Steps stored it; the block shows it. */
	browserChip?: BrowserChip;
}

/** Where a run's range read stands; keyed by the run's newest row id. */
export interface RunLoad {
	/** The oldest row read so far; the next range page continues before it. */
	oldestId: string | null;
	/** Rows older than `oldestId` inside the range are still unread. */
	hasMore: boolean;
	loading: boolean;
	error: boolean;
	/** The stub's oldest row when the range was last read, to notice a run that grew older. */
	coveredFirstId: string;
}

/** A Focus page's `runs` field, as it arrives. */
export interface FocusRunsPage {
	runs?: unknown;
}

function isStub(raw: any): boolean {
	return raw !== null && typeof raw === 'object' && raw.kind === 'run'
		&& typeof raw.first_id === 'string' && typeof raw.last_id === 'string'
		&& Number.isInteger(raw.first_seq) && Number.isInteger(raw.last_seq);
}

/** The stubs of one Focus page, oldest first. Malformed entries are dropped, never half-read. */
export function parseRunStubs(page: FocusRunsPage | null | undefined): RunStub[] {
	const raw = Array.isArray(page?.runs) ? page!.runs as any[] : [];
	return raw.filter(isStub).map((stub) => ({
		firstId: stub.first_id,
		lastId: stub.last_id,
		firstSeq: stub.first_seq,
		lastSeq: stub.last_seq,
		count: Number.isInteger(stub.count) ? stub.count : 0,
		turnIds: Array.isArray(stub.turn_ids)
			? stub.turn_ids.filter((id: unknown): id is string => typeof id === 'string') : [],
		weight: typeof stub.weight === 'number' ? stub.weight : 0,
		kinds: Array.isArray(stub.kinds)
			? [...new Set(stub.kinds.filter((kind: unknown): kind is string => typeof kind === 'string'))].sort() as string[] : [],
		continuesOlder: stub.continues_older === true,
		browserChip: parseBrowserChip(stub.browser_chip) ?? undefined,
	})).sort((a, b) => a.firstSeq - b.firstSeq);
}

/**
 * Stubs with no visible row between them are one run: their ranges join into one.
 * The run continues older only if its oldest piece does: a cut newer piece says nothing about the start.
 */
export function combineStubs(stubs: readonly RunStub[]): RunStub {
	const ordered = [...stubs].sort((a, b) => a.firstSeq - b.firstSeq);
	const oldest = ordered[0];
	const newest = ordered[ordered.length - 1];
	const turnIds: string[] = [];
	for (const id of ordered.flatMap((stub) => stub.turnIds)) {
		if (turnIds[turnIds.length - 1] !== id) turnIds.push(id);
	}
	return {
		firstId: oldest.firstId,
		lastId: newest.lastId,
		firstSeq: oldest.firstSeq,
		lastSeq: newest.lastSeq,
		count: ordered.reduce((total, stub) => total + stub.count, 0),
		turnIds,
		weight: ordered.reduce((total, stub) => total + stub.weight, 0),
		kinds: [...new Set(ordered.flatMap((stub) => stub.kinds))].sort(),
		continuesOlder: oldest.continuesOlder,
	};
}

/** The tokens that mark a run open: its ends, which a later page never changes for the newer end. */
export function stubTokens(stub: RunStub): string[] {
	return [`row:${stub.firstId}`, `row:${stub.lastId}`];
}

/**
 * The newest page replaces the stubs it covers; stubs older than everything it
 * scanned (read by earlier older-page loads) are kept.
 */
export function adoptFirstRuns(previous: readonly RunStub[], page: readonly RunStub[], oldestSeq: number | null): RunStub[] {
	const older = oldestSeq === null ? [] : previous.filter((stub) => stub.lastSeq < oldestSeq);
	return [...older, ...page];
}

/** An older page adds its stubs; one the list already holds is not added twice. */
export function mergeOlderRuns(previous: readonly RunStub[], page: readonly RunStub[]): RunStub[] {
	const known = new Set(previous.map((stub) => stub.lastId));
	return [...previous, ...page.filter((stub) => !known.has(stub.lastId))]
		.sort((a, b) => a.firstSeq - b.firstSeq);
}

/** Whether a run's range read has more to fetch, noticing a run that grew older after it was read. */
export function runHasMore(stub: RunStub, load: RunLoad | undefined): boolean {
	if (!load) return true;
	return load.hasMore || load.coveredFirstId !== stub.firstId;
}

/** The runs after a first-page read: a Focus page's stubs, the older ones kept; a plain page has none. */
export function adoptFirstPageRuns(
	previous: readonly RunStub[],
	data: FocusRunsPage | null | undefined,
	rows: readonly { seq?: number | null }[],
): RunStub[] {
	if (!Array.isArray(data?.runs)) return [];
	const page = parseRunStubs(data);
	const seqs = [...rows.map((row) => row.seq), ...page.map((stub) => stub.firstSeq)]
		.filter((seq): seq is number => typeof seq === 'number' && seq > 0);
	return adoptFirstRuns(previous, page, seqs.length ? Math.min(...seqs) : null);
}

/** The runs after an older-page read. */
export function adoptOlderPageRuns(previous: readonly RunStub[], data: FocusRunsPage | null | undefined): RunStub[] {
	return Array.isArray(data?.runs) ? mergeOlderRuns(previous, parseRunStubs(data)) : [...previous];
}
