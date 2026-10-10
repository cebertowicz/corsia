import type { UpdateCard } from '$lib/api/threadUpdates';
import { buildTranscriptPresentation, type TranscriptEntry, type TranscriptMessage } from '$lib/components/messageListModel';
import type { TurnSummary } from '$lib/components/transcriptRunLabel';
import { mergeTranscriptUpdates } from '$lib/components/transcriptUpdates';

export const THREAD = 'thread-1';
/** Naive UTC, the form most server writers store. */
export const DAY = '2026-10-02T';

/** One hidden step of work, counted by the server as `weight` (null: a live frame, which carries no weight). */
export function work(id: string, seq: number, at: string, turn = 't1', weight: number | null = 1): TranscriptMessage {
	return {
		id, thread_id: THREAD, role: 'assistant', content: '', seq, created_at: DAY + at,
		...(weight === null ? {} : { activity_weight: weight }), toolHints: ['Read a file'], metadata: { turn_id: turn, tool_hints: ['Read a file'] },
	};
}

/** A `post_update` message: said to you, so it closes a run. */
export function said(id: string, seq: number, at: string, turn = 't1'): TranscriptMessage {
	return {
		id, thread_id: THREAD, role: 'assistant', content: `said ${id}`, seq, created_at: DAY + at,
		activity_weight: 0, metadata: { turn_id: turn, for_user: true },
	};
}

/** A human input: `at` is when it was admitted, `written` when it reached the provider. */
export function human(id: string, seq: number, at: string, turn: string | null, written: string | null): TranscriptMessage {
	return {
		id, thread_id: THREAD, role: 'user', content: `ask ${id}`, seq, created_at: DAY + at,
		activity_weight: 0, active_turn_id: turn, delivery_state: 'delivered', write_started_at: written === null ? null : DAY + written, metadata: {},
	};
}

/** A hidden agent input (a wake): it sits inside the run. */
export function wake(id: string, seq: number, at: string, turn: string | null): TranscriptMessage {
	return { ...human(id, seq, at, turn, at), metadata: { sender_kind: 'agent' } };
}

/** A system notice; the three types whose writers were read stamp the row as they insert it. */
export function notice(id: string, seq: number, at: string, type = 'cc_error'): TranscriptMessage {
	return {
		id, thread_id: THREAD, role: 'system', content: `notice ${id}`, seq, created_at: DAY + at,
		activity_weight: 0, metadata: { type },
	};
}

export function turn(start: string, end: string | null, count = 0, status = 'completed'): TurnSummary {
	return { started_at: DAY + start, ended_at: end === null ? null : DAY + end, status, activity_count: count };
}

/** A plan update card; no production writer records one, so its stamp proves nothing. */
export function card(id: number, afterSeq: number, nodeId: string | null = `node-${id}`, at = '10:05:00'): UpdateCard {
	return {
		id, thread_id: THREAD, kind: 'failed', node_id: nodeId, title: `Item ${id}`,
		after_seq: afterSeq, created_at: DAY + at, node_exists: true, asks_user: true,
	} as UpdateCard;
}

export function present(
	rows: TranscriptMessage[],
	options: { live?: { turnId: string | null } | null; cards?: UpdateCard[] } = {},
): TranscriptEntry[] {
	return mergeTranscriptUpdates(
		buildTranscriptPresentation(rows, true, options.live ?? null),
		options.cards ?? [],
		{ focusMode: true, divider: null },
	);
}

/** The label of every finished run, in transcript order. */
export function runFaces(entries: TranscriptEntry[], labels: Map<string, string>): string[] {
	return entries.flatMap((entry) => (entry.kind === 'fold' && !entry.live ? [labels.get(entry.id) ?? ''] : []));
}

/** The `seq` of each run's rows, in transcript order. */
export function runSeqs(entries: TranscriptEntry[]): number[][] {
	return entries.flatMap((entry) => (
		entry.kind === 'fold' ? [entry.items.map((item) => item.message.seq as number)] : []
	));
}

/** Runs `body` with the process in `zone`, as a browser in that zone would format a clock. */
export function inZone<T>(zone: string, body: () => T): T {
	const previous = process.env.TZ;
	process.env.TZ = zone;
	try {
		return body();
	} finally {
		if (previous === undefined) delete process.env.TZ;
		else process.env.TZ = previous;
	}
}
