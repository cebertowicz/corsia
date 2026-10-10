import { PLAN_LIMIT_REASON } from '$lib/stores/overageSetting';
import type { TranscriptMessage } from './messageListModel';
import { isKnownFutureInput, sourceMembers, sourceRange, type SourcePlacementContext } from './contextEventModel';
import type { TranscriptEntry, TranscriptFoldEntry, TranscriptMessageEntry, TranscriptPauseEntry } from './transcriptFocusModel';

export type PlanLimitPauseDescriptor = {
	stoppedTurnId: string;
	source: TranscriptMessage;
	anchorKind: 'input' | 'work';
};

type PauseSummary = { status?: string; cause?: string | null };

/** The one test for "this Turn stopped at the plan limit", shared by loaded rows and stubs. */
export function isPlanLimitPause(summary: PauseSummary | undefined): boolean {
	return summary?.status === 'cancelled' && summary.cause === PLAN_LIMIT_REASON;
}
const SETTLED_INPUTS = new Set(['delivered', 'ambiguous', 'failed', 'cancelled']);

function isInput(row: TranscriptMessage): boolean {
	return row.role === 'user' || row.metadata?.type === 'context_event'
		|| typeof row.active_turn_id === 'string';
}

function ownersOf(row: TranscriptMessage): string[] {
	const ids = [row.metadata?.turn_id, isInput(row) ? row.active_turn_id : null];
	return [...new Set(ids.filter((id): id is string => typeof id === 'string' && id.length > 0))];
}

function isSource(row: TranscriptMessage): boolean {
	return row.metadata?.type !== 'seed' && !(row.role === 'user' && row.metadata?.routed_event);
}

function isEligible(row: TranscriptMessage): boolean {
	return row._optimistic !== true && (!isInput(row) || SETTLED_INPUTS.has(row.delivery_state));
}

/** Canonical stopped-Turn attribution, from the raw loaded snapshot before visibility. */
export function planLimitPauseDescriptors(
	rows: readonly TranscriptMessage[],
	summaries: Record<string, PauseSummary>,
): PlanLimitPauseDescriptor[] {
	const invalidTurns = new Set<string>();
	const invalidate = (row: TranscriptMessage) => ownersOf(row).forEach((id) => invalidTurns.add(id));
	const sources = rows.filter(isSource);
	const atSeq = new Map<number, TranscriptMessage>();
	let previous: TranscriptMessage | undefined;
	for (const row of sources) {
		if (ownersOf(row).length > 1 || !isEligible(row)) invalidate(row);
		if (!Number.isInteger(row.seq) || row.seq <= 0) { invalidate(row); continue; }
		const duplicate = atSeq.get(row.seq);
		if (duplicate) { invalidate(row); invalidate(duplicate); }
		if (previous && row.seq <= previous.seq) { invalidate(row); invalidate(previous); }
		atSeq.set(row.seq, row);
		previous = row;
	}
	const last = new Map<string, PlanLimitPauseDescriptor>();
	for (const row of sources) {
		const [turnId] = ownersOf(row);
		if (!turnId || invalidTurns.has(turnId) || !isPlanLimitPause(summaries[turnId])) continue;
		last.set(turnId, { stoppedTurnId: turnId, source: row, anchorKind: isInput(row) ? 'input' : 'work' });
	}
	return [...last.values()].sort((left, right) => left.source.seq - right.source.seq);
}

function leaves(entry: TranscriptEntry): TranscriptMessageEntry[] {
	return entry.kind === 'fold' ? entry.items : entry.kind === 'message' ? [entry] : [];
}

/** Last presentation phase of a represented raw source (content follows activity). */
function representedSlot(entries: TranscriptEntry[], pause: PlanLimitPauseDescriptor): { entry: number; item: number } | null {
	for (let index = entries.length - 1; index >= 0; index -= 1) {
		const items = leaves(entries[index]);
		for (let item = items.length - 1; item >= 0; item -= 1) {
			if (sourceMembers(items[item].message).some((row) => row.id === pause.source.id)) {
				return sourceRange(items[item].message)?.max === pause.source.seq ? { entry: index, item } : null;
			}
		}
	}
	return null;
}

type ItemPositions = { positioned: { item: number; min: number; max: number }[]; future: boolean };

/** Validate one host's numeric prefix and canonical future suffix, retaining leaf indices. */
function itemPositions(items: TranscriptMessageEntry[], context?: SourcePlacementContext): ItemPositions | null {
	const positioned: ItemPositions['positioned'] = [];
	let future = false;
	for (let item = 0; item < items.length; item += 1) {
		const range = sourceRange(items[item].message);
		if (range) {
			const previous = positioned[positioned.length - 1];
			if (future || (previous && previous.max >= range.min)) return null;
			positioned.push({ item, ...range });
		} else {
			if (!isKnownFutureInput(items[item].message, context)) return null;
			future = true;
		}
	}
	return { positioned, future };
}

function suppressedRunSlot(entry: TranscriptFoldEntry, seq: number, context?: SourcePlacementContext): number | null {
	const positions = itemPositions(entry.items, context);
	if (!positions || positions.positioned.length < 2) return null;
	const ranges = positions.positioned;
	if (ranges[0].min >= seq || ranges[ranges.length - 1].max <= seq) return null;
	const next = ranges.findIndex((range) => range.min > seq);
	if (next < 1 || ranges[next - 1].max >= seq) return null;
	return ranges[next - 1].item;
}

/** The fixed visit boundary constrains every standalone path. */
function dividerBounds(entries: TranscriptEntry[], seq: number): { after: number; before: number } | null {
	let after = 0;
	let before = entries.length;
	for (let index = 0; index < entries.length; index += 1) {
		const entry = entries[index];
		if (entry.kind !== 'divider') continue;
		const boundary = entry.first_seq;
		if (boundary == null || !Number.isInteger(boundary) || boundary <= 0) return null;
		if (seq >= boundary) after = Math.max(after, index + 1);
		else before = Math.min(before, index);
	}
	return after <= before ? { after, before } : null;
}

function representedStandaloneSlot(entries: TranscriptEntry[], seq: number, sourceEntry: number): number | null {
	const bounds = dividerBounds(entries, seq);
	if (!bounds) return null;
	let slot = sourceEntry + 1;
	while (slot < bounds.after && entries[slot]?.kind === 'divider') slot += 1;
	return slot >= bounds.after && slot <= bounds.before ? slot : null;
}

/** A standalone slot must agree with every structural row/card, including equal cards. */
function standaloneSlot(entries: TranscriptEntry[], seq: number, context?: SourcePlacementContext): number | null {
	const bounds = dividerBounds(entries, seq);
	if (!bounds) return null;
	let { after, before } = bounds;
	for (let index = 0; index < entries.length; index += 1) {
		const entry = entries[index];
		if (entry.kind === 'divider' || entry.kind === 'passive' || entry.kind === 'pause' || entry.kind === 'stub-pause') continue;
		if (entry.kind === 'update') {
			if (entry.card.after_seq < seq) after = Math.max(after, index + 1);
			else before = Math.min(before, index);
			continue;
		}
		const positions = itemPositions(leaves(entry), context);
		if (!positions) return null;
		const { positioned, future } = positions;
		if (!positioned.length && !future) { before = Math.min(before, index); continue; }
		const min = positioned[0]?.min ?? null;
		if (future) {
			if (min !== null && min <= seq) return null;
			before = Math.min(before, index);
			continue;
		}
		const max = positioned[positioned.length - 1].max;
		if (max <= seq) after = Math.max(after, index + 1);
		else if (min !== null && min > seq) before = Math.min(before, index);
		else return null;
	}
	if (after > before) return null;
	// Tucked collections belong to the next item; never cross a constrained divider.
	let slot = before;
	while (slot > after && (entries[slot - 1].kind === 'divider' || entries[slot - 1].kind === 'passive')) slot -= 1;
	return slot;
}

/** Pure composition: annotations add no work leaf, run boundary, weight or token. */
export function attachPlanLimitPauses(
	entries: TranscriptEntry[],
	pauses: readonly PlanLimitPauseDescriptor[],
	context?: SourcePlacementContext,
): { entries: TranscriptEntry[]; unavailable: PlanLimitPauseDescriptor[] } {
	if (!pauses.length) return { entries, unavailable: [] };
	const result = entries.map((entry) => entry.kind === 'fold' ? { ...entry, pauses: [] } as TranscriptFoldEntry : entry);
	const standalone = new Map<number, TranscriptPauseEntry[]>();
	const unavailable: PlanLimitPauseDescriptor[] = [];
	for (const pause of pauses) {
		const represented = representedSlot(entries, pause);
		let runIndex = represented?.entry ?? -1;
		let afterItem = represented?.item ?? -1;
		if (!represented) {
			for (let index = 0; index < entries.length; index += 1) {
				const entry = entries[index];
				if (entry.kind !== 'fold') continue;
				const item = suppressedRunSlot(entry, pause.source.seq, context);
				if (item === null) continue;
				runIndex = index;
				afterItem = item;
				break;
			}
		}
		const host = result[runIndex];
		if (host?.kind === 'fold') {
			host.pauses!.push({ pause, showProxy: !represented, afterItem });
			continue;
		}
		const slot = represented
			? representedStandaloneSlot(entries, pause.source.seq, represented.entry)
			: standaloneSlot(entries, pause.source.seq, context);
		if (slot === null) { unavailable.push(pause); continue; }
		const annotations = standalone.get(slot) ?? [];
		annotations.push({ kind: 'pause', id: `pause:${pause.stoppedTurnId}:${pause.source.id}`, pause, showProxy: !represented });
		standalone.set(slot, annotations);
	}
	return { entries: result.flatMap((entry, index) => [...standalone.get(index) ?? [], entry]).concat(standalone.get(result.length) ?? []), unavailable };
}

/**
 * Pause markers for Turns whose rows sit only in unloaded stubs: the stub's turn ids and the
 * page's turn summaries are enough to say the Turn stopped, so no source row is invented.
 * Returns, per fold entry id, the Turns to mark after it (the last stub that holds the Turn).
 */
export function stubPauseTurns(
	entries: readonly TranscriptEntry[],
	pauses: readonly PlanLimitPauseDescriptor[],
	summaries: Record<string, PauseSummary>,
): Map<string, string[]> {
	const covered = new Set([
		...pauses.map((pause) => pause.stoppedTurnId),
		...entries.flatMap((entry) => entry.kind === 'stub-pause' ? [entry.turnId] : entry.kind === 'pause'
			? [entry.pause.stoppedTurnId] : entry.kind === 'fold' ? (entry.pauses ?? []).map((annotation) => annotation.pause.stoppedTurnId) : []),
	]);
	const hostOf = new Map<string, string>();
	for (const entry of entries) {
		if (entry.kind !== 'fold' || !entry.stub) continue;
		for (const turnId of entry.stub.turnIds) {
			if (!covered.has(turnId) && isPlanLimitPause(summaries[turnId])) hostOf.set(turnId, entry.id);
		}
	}
	const byEntry = new Map<string, string[]>();
	for (const [turnId, entryId] of hostOf) byEntry.set(entryId, [...byEntry.get(entryId) ?? [], turnId]);
	return byEntry;
}
