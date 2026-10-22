import { parseWsTimestamp } from '$lib/time';
import type { TranscriptMessage } from './messageListModel';
import { runKinds, type RowKind, type TranscriptEntry, type TranscriptFoldEntry } from './transcriptFocusModel';
import { beside, boundOf, clampTo, turnOf, type Bounds, type Turn } from './transcriptRunTimes';

export type TurnSummary = {
	started_at: string | null;
	ended_at: string | null;
	status: string;
	activity_count: number;
	cause?: string | null;
};

export type TurnSummaries = Record<string, TurnSummary>;

/** What a run says when a fact it would show is not proven. */
const FALLBACK = 'Worked';

/** What a block with no steps calls the one kind of row it holds (A14, A15, A15b). */
const KIND_NAMES: Partial<Record<RowKind, string>> = {
	context_update: 'Context updated',
	account_move: 'Account moved',
	model_switch: 'Model switched',
	subagent_finished: 'Subagent finished',
	agent_message: 'Agent message',
};

/** Appended to a block's label while it holds something new since the last visit (F2a). */
export const NEW_SUFFIX = ' · new';

/** The one duration form: `under 1 min`, `N min`, `N h M min`, `N h` (A7, A8). */
export function formatDuration(elapsed: number): string {
	const minutes = Math.floor(elapsed / 60_000);
	if (minutes < 1) return 'under 1 min';
	const [hours, rest] = [Math.floor(minutes / 60), minutes % 60];
	if (hours < 1) return `${minutes} min`;
	return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

function formatCount(count: number): string {
	return count.toLocaleString();
}

/** The summed server weight of a run's rows, or null as soon as one is unknown: never a partial sum. */
export function runWeight(run: TranscriptFoldEntry): number | null {
	// A stub's weight already covers every row of its range, loaded or not.
	const { stub } = run;
	let total = stub?.weight ?? 0;
	for (const item of run.items) {
		const { seq } = item.message;
		if (stub && typeof seq === 'number' && seq >= stub.firstSeq && seq <= stub.lastSeq) continue;
		const weight = item.message.activity_weight;
		if (typeof weight !== 'number') return null;
		total += weight;
	}
	return total;
}

/** Rows the block holds: its stub's count plus loaded rows past the stub, plus its card lines. */
function rowCount(run: TranscriptFoldEntry): number {
	const { stub } = run;
	const outside = run.items.filter(({ message }) => !(stub && typeof message.seq === 'number'
		&& message.seq >= stub.firstSeq && message.seq <= stub.lastSeq)).length;
	return (stub?.count ?? 0) + outside + (run.cards?.length ?? 0);
}

/** A block with no steps names what it holds (A14, A15, A15b); never a bare `Worked` once its kinds are known. */
function bookkeepingLabel(run: TranscriptFoldEntry): string {
	const kinds = runKinds(run).filter((kind) => kind !== 'steps');
	const names = kinds.flatMap((kind) => KIND_NAMES[kind] ?? []);
	if (kinds.length === 0 && !run.cards?.length) return FALLBACK;
	if (names.length === kinds.length && names.length > 0 && names.length <= 2) return names.join(' · ');
	const events = rowCount(run);
	return `${formatCount(events)} ${events === 1 ? 'event' : 'events'}`;
}

function label(
	run: TranscriptFoldEntry,
	startKnown: boolean,
	elapsed: number,
): string {
	const count = runWeight(run);
	// Rows above the unread part of the block may hold steps and time: show what is proven, as a floor.
	if (!startKnown) return count ? `Worked · ${formatCount(count)}+ steps` : FALLBACK;
	if (count === 0) return bookkeepingLabel(run);
	const parts = [FALLBACK];
	if (count) parts.push(`${formatCount(count)} ${count === 1 ? 'step' : 'steps'}`);
	if (Number.isFinite(elapsed)) parts.push(formatDuration(elapsed));
	return parts.join(' · ');
}

type Span = {
	run: TranscriptFoldEntry;
	startKnown: boolean;
	lower: number;
	upper: number;
	/** Whether either clipped bound failed the Turn's order check. */
	fails: () => boolean;
};

/** Where each block starts and ends, by the items around it; shared by finished and live labels. */
function spansOf(
	entries: TranscriptEntry[],
	turnSummaries: TurnSummaries,
	historyComplete: boolean,
): Span[] {
	const spans: { run: TranscriptFoldEntry; startKnown: boolean; lower: number; upper: number }[] = [];
	const proven = new Map<string, number[]>();
	let rowsBefore = false;
	entries.forEach((entry, index) => {
		if (entry.kind === 'fold') {
			const first = entry.turnIds[0];
			const last = entry.turnIds[entry.turnIds.length - 1];
			const firstTurn = first === undefined ? null : turnOf(turnSummaries[first]);
			const lastTurn = last === undefined ? null : turnOf(turnSummaries[last]);
			let lower = Number.NaN;
			let upper = Number.NaN;
			if (firstTurn) lower = boundOf(entries, beside(entries, index, -1), 'lower', first, firstTurn, historyComplete);
			if (lastTurn && !entry.live) upper = boundOf(entries, beside(entries, index, 1), 'upper', last, lastTurn, historyComplete);
			// Only a finished run's proven stamps enter the order check of its Turns.
			if (!entry.live) {
				if (first !== undefined) proven.set(first, [...proven.get(first) ?? [], ...[lower].filter(Number.isFinite)]);
				if (last !== undefined) proven.set(last, [...proven.get(last) ?? [], ...[upper].filter(Number.isFinite)]);
			}
			// A stub says whether its run continues past the page; only a stub-less run falls back to its position.
			const startKnown = entry.stub ? !entry.stub.continuesOlder : historyComplete || rowsBefore;
			spans.push({
				run: entry, startKnown,
				lower: firstTurn ? clampTo(firstTurn, lower) : Number.NaN,
				upper: lastTurn ? clampTo(lastTurn, upper) : Number.NaN,
			});
		}
		if (entry.kind === 'fold' || entry.kind === 'message') rowsBefore = true;
	});
	const timeFails = (turnId: string) => (
		(proven.get(turnId) ?? []).some((value, at, all) => at > 0 && value < all[at - 1])
	);
	return spans.map((span) => ({
		...span,
		fails: () => span.run.turnIds.length > 0
			&& (timeFails(span.run.turnIds[0]) || timeFails(span.run.turnIds[span.run.turnIds.length - 1])),
	}));
}

/**
 * A run's range for the server's open time: from where it starts to where it ends, or to now while it is
 * open (`until` null). `id` is the run's entry id.
 */
export type RunRange = { id: string; since: number; until: number | null };

/** What the server says a range's open time is, in ms, or null before it has answered. */
export type OpenMs = (range: RunRange) => number | null;

/** Where each block's open time is read: only a run whose start and end the shown items prove has a range. */
export function runRanges(
	entries: TranscriptEntry[],
	turnSummaries: TurnSummaries,
	historyComplete: boolean,
): RunRange[] {
	return spansOf(entries, turnSummaries, historyComplete).flatMap((span): RunRange[] => {
		if (!span.startKnown || span.fails() || !Number.isFinite(span.lower)) return [];
		if (span.run.live) return [{ id: span.run.id, since: span.lower, until: null }];
		return Number.isFinite(span.upper) ? [{ id: span.run.id, since: span.lower, until: span.upper }] : [];
	});
}

/**
 * The range of an outside wait's number: the open run it waits in, or, standing alone after a reply, from
 * that reply. Null when the shown items prove no start, and the chip drops the number rather than guess (EW8).
 */
export function waitRange(
	entries: TranscriptEntry[],
	turnSummaries: TurnSummaries,
	historyComplete: boolean,
): RunRange | null {
	const last = entries.at(-1);
	if (last?.kind === 'fold') {
		const range = runRanges(entries, turnSummaries, historyComplete).find(({ id }) => id === last.id);
		return range ? { ...range, until: null } : null;
	}
	if (last?.kind !== 'message' || last.message.role !== 'assistant') return null;
	const since = parseWsTimestamp(last.message.created_at);
	return Number.isFinite(since) ? { id: last.message.id, since, until: null } : null;
}

/**
 * The label of every finished block in one loaded presentation, by entry id:
 * `Worked · <N> steps · <open time>`, or the part of it that is proven (A1-A16).
 *
 * A block's steps are its rows' server weights. Its time is the server's open time for the
 * block's range (`runRanges`); it shows none until the server has answered.
 */
export function runLabels(
	entries: TranscriptEntry[],
	turnSummaries: TurnSummaries,
	historyComplete: boolean,
	openMs: OpenMs = () => null,
): Map<string, string> {
	const ranges = new Map(runRanges(entries, turnSummaries, historyComplete).map((range) => [range.id, range]));
	return new Map(spansOf(entries, turnSummaries, historyComplete)
		.filter(({ run }) => !run.live)
		.map((span) => {
			const range = ranges.get(span.run.id);
			const elapsed = range ? openMs(range) : null;
			return [span.run.id, `${label(span.run, span.startKnown, elapsed ?? Number.NaN)}${span.run.new ? NEW_SUFFIX : ''}`];
		}));
}

/**
 * The open time so far of every live block, by entry id, in the block's own form (`3 min`), or null
 * while its start is not proven or the server has not answered (A9-A12). The surface joins it with
 * the current phrase through `liveLabel`.
 */
export function liveRunTimes(
	entries: TranscriptEntry[],
	turnSummaries: TurnSummaries,
	historyComplete: boolean,
	openMs: OpenMs = () => null,
): Map<string, string | null> {
	const times = new Map<string, string | null>();
	for (const entry of entries) if (entry.kind === 'fold' && entry.live) times.set(entry.id, null);
	for (const range of runRanges(entries, turnSummaries, historyComplete)) {
		if (!times.has(range.id)) continue;
		const elapsed = openMs(range);
		times.set(range.id, elapsed === null ? null : formatDuration(elapsed));
	}
	return times;
}

/** The wait chip's "so far" number in the run chip's own form, or null while unproven or unanswered. */
export function waitElapsed(range: RunRange | null, openMs: OpenMs): string | null {
	const elapsed = range ? openMs(range) : null;
	return elapsed === null ? null : formatDuration(elapsed);
}

/** `Working · 3 min · Reading files`; whatever is unknown drops off the end (A9-A11). */
export function liveLabel(time: string | null, phrase: string | null): string {
	return ['Working', time, phrase].filter(Boolean).join(' · ') || 'Working';
}

/** What a block's toggle says to a screen reader: the verb stays, a new block says so (F2). */
export function runToggleName(run: TranscriptFoldEntry, label: string, expanded: boolean): string {
	const plain = label.endsWith(NEW_SUFFIX) ? label.slice(0, -NEW_SUFFIX.length) : label;
	return `${expanded ? 'Collapse' : 'Expand'} ${plain}${run.new ? ', new since your last visit' : ''}`;
}
