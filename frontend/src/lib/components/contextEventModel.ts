import { humanTokens } from '$lib/format';
import type { TranscriptMessage } from './messageListModel';
import { isAwaitingDelivery, isQueuedAfterTurn, isQueuedInputOwnedByActiveTurn } from '$lib/messageDelivery';
import type { TurnPhase } from '$lib/ws/turnState';

export function isPassiveContextEvent(msg: TranscriptMessage): boolean {
	return msg.role === 'system' && msg.metadata?.type === 'context_event';
}

export function isActivityReceipt(msg: TranscriptMessage): boolean {
	return msg.role === 'system' && msg.metadata?.type === 'activity_receipt';
}

export function isPendingPassiveContextEvent(msg: TranscriptMessage): boolean {
	return isPassiveContextEvent(msg)
		&& msg.metadata?.event_disposition === 'passive'
		&& msg.delivery_state === 'queued';
}

export function isCompactTranscriptEvent(msg: TranscriptMessage): boolean {
	return isPassiveContextEvent(msg) || isActivityReceipt(msg);
}

/** View-only raw references: never serialized as transcript details or API state. */
export const SOURCE_MEMBERS = Symbol('transcriptSourceMembers');

type MemberRow = TranscriptMessage & { [SOURCE_MEMBERS]?: readonly TranscriptMessage[] };

/** A stored digest is one atom; only frontend aggregates flatten source members. */
export function sourceMembers(row: TranscriptMessage): readonly TranscriptMessage[] {
	return (row as MemberRow)[SOURCE_MEMBERS] ?? [row];
}

export function sourceRange(row: TranscriptMessage): { min: number; max: number } | null {
	const members = sourceMembers(row);
	if (!members.length || members.some((member, index) => (
		!Number.isInteger(member.seq) || member.seq <= 0
		|| (index > 0 && member.seq <= members[index - 1].seq)
	))) return null;
	return { min: members[0].seq, max: members[members.length - 1].seq };
}

export type SourcePlacementContext = { threadId: string; phase: TurnPhase; turnId: string | null };

/** A future input is a main-view fact, never an invented durable sequence. */
export function isKnownFutureInput(row: TranscriptMessage, context?: SourcePlacementContext): boolean {
	const members = sourceMembers(row);
	if (!context || members.length !== 1) return false;
	const input = members[0];
	if (input.seq != null || input.thread_id !== context.threadId
		|| (input.role !== 'user' && !isPassiveContextEvent(input))) return false;
	if (isAwaitingDelivery(input, context.phase === 'active') || isQueuedAfterTurn(input)) return false;
	if (context.phase !== 'active') return input._optimistic === true && input.delivery_state == null
		&& input.active_turn_id == null && input.metadata?.turn_id == null;
	return context.phase === 'active' && context.turnId !== null
		&& input._optimistic !== true && input.delivery_error == null
		&& isQueuedInputOwnedByActiveTurn(input) && input.active_turn_id === context.turnId
		&& (input.metadata?.turn_id == null || input.metadata.turn_id === context.turnId);
}

/** Keep final/pending and positioned/unpositioned rows from sharing an aggregate. */
export function canMergeRows(left: TranscriptMessage, right: TranscriptMessage): boolean {
	return (left.delivery_state == null || left.delivery_state === 'delivered')
		&& (right.delivery_state == null || right.delivery_state === 'delivered')
		&& left._optimistic !== true && right._optimistic !== true
		&& Boolean(sourceRange(left)) === Boolean(sourceRange(right));
}

/** Every represented id/member and the complete server weight (unknown if any is unknown). */
export function mergedRowFacts(
	group: TranscriptMessage[],
): { mergedIds: string[]; activity_weight: number | undefined; [SOURCE_MEMBERS]: readonly TranscriptMessage[] } {
	const weights = group.map((row) => row.activity_weight);
	return {
		[SOURCE_MEMBERS]: group.flatMap((row) => [...sourceMembers(row)]),
		mergedIds: group.flatMap((row) => row.mergedIds ?? [row.id]),
		activity_weight: weights.every((weight) => typeof weight === 'number')
			? weights.reduce((total, weight) => total + weight, 0) : undefined,
	};
}

/**
 * The one line a digest's event shows in the transcript, or null for no row.
 *
 * `summary` is what the agent reads. A producer that wants the chip to say
 * something else sets `chip`; `chip: null` marks routine state only the agent
 * needs. No `chip` means the summary is already a line for a person, which is
 * also how a digest stored before the field existed reads: history keeps the
 * line it was journaled with. A wake row's line is `metadata.summary`; the
 * producer owns both shapes and this model is their one reader.
 */
function chipLine(event: Record<string, any>): string | null {
	if (event.chip === null) return null;
	if (typeof event.chip === 'string' && event.chip) return event.chip;
	return String(event.summary || event.content || 'Context update');
}

function isDigestWithEvents(msg: TranscriptMessage): boolean {
	return msg.metadata?.event_type === 'context.digest'
		&& Array.isArray(msg.metadata?.context_events);
}

/**
 * The rows one transcript message contributes to a Context chip's details.
 *
 * A backend digest is already a list of updates, so it contributes those
 * updates, not itself: a chip never nests another chip's count. Updates that
 * show no line are dropped.
 */
export function contextEventRows(msg: TranscriptMessage): Record<string, unknown>[] {
	if (!isDigestWithEvents(msg)) {
		return [{
			id: msg.id,
			summary: msg.metadata?.summary || msg.content || 'Context update',
			created_at: msg.created_at,
			event_type: msg.metadata?.event_type,
		}];
	}
	return (msg.metadata.context_events as Record<string, any>[]).flatMap((event) => {
		const summary = chipLine(event);
		return summary === null ? [] : [{
			id: event.id,
			summary,
			created_at: event.created_at,
			event_type: event.event_type ?? event.change_kind,
		}];
	});
}

function countLabel(rows: Record<string, unknown>[]): string {
	return rows.length === 1 ? String(rows[0].summary) : `${rows.length} context updates`;
}

/**
 * A backend digest as the transcript shows it: its shown rows and a header
 * that counts exactly them, or null when nothing in it is for a person.
 * Anything else passes through.
 */
export function presentContextDigest(msg: TranscriptMessage): TranscriptMessage | null {
	if (!isDigestWithEvents(msg)) return msg;
	const rows = contextEventRows(msg);
	if (rows.length === 0) return null;
	return {
		...msg,
		[SOURCE_MEMBERS]: sourceMembers(msg),
		metadata: { ...msg.metadata, summary: countLabel(rows), context_events: rows },
	};
}

export function summarizeContextEvents(group: TranscriptMessage[]): string {
	const fileUpdates = group.filter(
		(msg) => msg.metadata?.source_label || msg.metadata?.source_path,
	).length;
	const artifactAdds = group.reduce((total, msg) => {
		const count = msg.metadata?.artifact_count_delta ?? msg.metadata?.artifact_count ?? 0;
		return total + (typeof count === 'number' && count > 0 ? count : 0);
	}, 0);
	const parts = [
		fileUpdates > 0
			? `${fileUpdates} context ${fileUpdates === 1 ? 'source' : 'sources'} updated`
			: '',
		artifactAdds > 0
			? `${artifactAdds} ${artifactAdds === 1 ? 'artifact' : 'artifacts'} added`
			: '',
	].filter(Boolean);
	return parts.length > 0 ? parts.join(', ') : countLabel(group.flatMap(contextEventRows));
}

export function buildPendingContextDigest(
	pending: TranscriptMessage[],
): TranscriptMessage | null {
	const rows = pending.flatMap((msg) => presentContextDigest(msg) ?? []);
	if (rows.length === 0) return null;
	if (rows.length === 1 && isDigestWithEvents(rows[0])) return rows[0];
	const summary = summarizeContextEvents(rows);
	return {
		id: 'pending-context-digest', ...mergedRowFacts(rows), thread_id: rows[0].thread_id,
		role: 'system', content: summary,
		metadata: { type: 'context_event', event_type: 'context.digest',
			event_disposition: 'passive', summary,
			context_events: rows.flatMap(contextEventRows) },
		created_at: rows.at(-1)?.created_at, delivery_state: 'queued',
	};
}

function contextTokenPair(meta: any): string | null {
	const total = Number(meta.total_tokens);
	const window = Number(meta.model_context_window);
	if (!Number.isFinite(total) || !Number.isFinite(window) || total < 0 || window <= 0) {
		return null;
	}
	return `${humanTokens(total)}/${humanTokens(window)}`;
}

export function passiveContextEventLabel(msg: TranscriptMessage): string {
	const meta = msg.metadata ?? {};
	const eventType = meta.event_type ?? '';
	if (eventType === 'context.digest' && typeof meta.summary === 'string') return meta.summary;
	const tokenPair = contextTokenPair(meta);
	const isBoundary = eventType === 'context.boundary'
		|| meta.trigger_reason === 'harness_model_switch';
	if (isBoundary) return tokenPair ? `Context boundary · ${tokenPair}` : 'Context boundary';
	if (eventType === 'context.compacted') {
		return tokenPair ? `Context compacted · ${tokenPair}` : 'Context compacted';
	}
	// An event that names its one line of news shows that line; the facts
	// beneath it are the model's input, not the chip's.
	if (typeof meta.summary === 'string' && meta.summary) return meta.summary;
	return String(msg.content ?? '');
}
