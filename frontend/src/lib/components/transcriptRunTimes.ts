import { parseWsTimestamp } from '$lib/time';
import type { TranscriptMessage } from './messageListModel';
import type { TranscriptEntry } from './transcriptFocusModel';
import type { TurnSummary } from './transcriptRunLabel';

// Notice types whose writers were read: four stamp the row as it is inserted, and
// the Codex terminal marker reads the clock just before its insert. None passes an
// earlier time. A notice of any other type proves no time, so the runs beside it show none.
const INSERT_STAMPED_NOTICES = new Set(['cc_error', 'tool_bridge_unavailable', 'subagent_done']);

export type Turn = { start: number; end: number; running: boolean };
type Side = 'lower' | 'upper';
export type Bounds = { lower: number; upper: number };

export function turnOf(summary: TurnSummary | undefined): Turn | null {
	if (!summary) return null;
	return {
		start: parseWsTimestamp(summary.started_at),
		end: parseWsTimestamp(summary.ended_at),
		running: summary.status === 'running' || summary.status === 'waiting_retry',
	};
}

function rowsOf(entry: TranscriptEntry): TranscriptMessage[] {
	if (entry.kind === 'message') return [entry.message];
	return entry.kind === 'fold' ? entry.items.map((item) => item.message) : [];
}

function belongsTo(row: TranscriptMessage, turnId: string): boolean {
	return row.metadata?.turn_id === turnId || row.active_turn_id === turnId;
}

function namesTurn(row: TranscriptMessage): boolean {
	return typeof row.metadata?.turn_id === 'string'
		|| (typeof row.active_turn_id === 'string' && row.active_turn_id !== '');
}

/**
 * Whether the input at `at` is where Turn `turnId` starts: the rows before it
 * are loaded back to a row of another Turn, or to the start of history, and
 * none of them is this Turn's.
 */
function startsTurn(
	entries: TranscriptEntry[],
	at: number,
	turnId: string,
	historyComplete: boolean,
): boolean {
	const earlier = entries.slice(0, at).flatMap(rowsOf);
	for (let index = earlier.length - 1; index >= 0; index -= 1) {
		if (belongsTo(earlier[index], turnId)) return false;
		if (namesTurn(earlier[index])) return true;
	}
	return historyComplete;
}

/** The entry beside `from`, past the visit divider, which is not a transcript item. */
export function beside(entries: TranscriptEntry[], from: number, step: 1 | -1): number {
	let at = from + step;
	while (entries[at] && ['divider', 'pause', 'stub-pause', 'passive'].includes(entries[at].kind)) at += step;
	return at;
}

/**
 * Where a run of Turn `turnId` starts (`lower`) or ends (`upper`), read from
 * the shown item beside it: that item's own stamp, the Turn's edge, or NaN
 * when the source proves neither. An unproven bound is never replaced by the
 * whole Turn, and no time is taken from `seq` or an input's admission time.
 */
export function boundOf(
	entries: TranscriptEntry[],
	at: number,
	side: Side,
	turnId: string,
	turn: Turn,
	historyComplete: boolean,
): number {
	const edge = side === 'lower' ? turn.start : turn.running ? Number.NaN : turn.end;
	const item = entries[at];
	// Nothing after the run: it ran to its Turn's end. Nothing before it: the Turn's start, once no older rows exist.
	if (!item) return side === 'upper' || historyComplete ? edge : Number.NaN;
	// An update card's stamp has no production writer to prove it.
	if (item.kind !== 'message') return Number.NaN;
	const row = item.message;
	const stamp = parseWsTimestamp(row.created_at);
	const beyond = side === 'lower' ? stamp < turn.start : stamp > turn.end;
	if (row.role === 'user') {
		const claimedBy = row.active_turn_id;
		if (typeof claimedBy !== 'string' || !claimedBy) return Number.NaN;
		if (claimedBy !== turnId) return edge;
		const written = parseWsTimestamp(row.write_started_at);
		if (Number.isFinite(written)) return written;
		// Without the write stamp only the Turn's start is provable, and only for the input that started it.
		return side === 'lower' && startsTurn(entries, at, turnId, historyComplete) ? edge : Number.NaN;
	}
	if (row.role === 'assistant') {
		if (row.metadata?.turn_id === turnId) return stamp;
		return beyond ? edge : Number.NaN;
	}
	if (!INSERT_STAMPED_NOTICES.has(String(row.metadata?.type ?? ''))) return Number.NaN;
	const within = stamp >= turn.start && (turn.running
		? entries.slice(at + 1).flatMap(rowsOf).some((later) => later.metadata?.turn_id === turnId)
		: stamp <= turn.end);
	if (within) return stamp;
	return beyond ? edge : Number.NaN;
}

export function clampTo(turn: Turn, value: number): number {
	return Math.min(Math.max(value, turn.start), Number.isFinite(turn.end) ? turn.end : Infinity);
}
