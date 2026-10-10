import type { VisitDivider } from '$lib/api/threadUpdates';
import { partitionMessageAtCards } from './messageListModel';
import { sourceRange, type SourcePlacementContext } from './contextEventModel';
import { attachPlanLimitPauses, stubPauseTurns, type PlanLimitPauseDescriptor } from './planLimitPause';
import { runEntry, type TranscriptEntry, type TranscriptFoldEntry, type TranscriptMessageEntry } from './transcriptFocusModel';

type PauseSummary = { status?: string; cause?: string | null };

/** Only a real visit boundary partitions loaded source members. Opaque stubs stay whole. */
export function partitionAtVisit(entries: TranscriptEntry[], divider: VisitDivider | null): TranscriptEntry[] {
	const first = divider?.first_seq;
	if (first == null || !Number.isInteger(first) || first <= 0) return entries;
	const cut = first - 1;
	const partition = (item: TranscriptMessageEntry) => partitionMessageAtCards(item.message, [cut]).map((message) => (
		message === item.message ? item : { ...item, id: `${item.id}:visit:${message.id}`, message }
	));
	return entries.flatMap<TranscriptEntry>((entry) => {
		if (entry.kind === 'message') return partition(entry);
		if (entry.kind !== 'fold' || (entry.stub && first <= entry.stub.lastSeq)) return [entry];
		// A block holding the visit stays whole (F2); only a merged row inside it is cut.
		return [{ ...entry, items: entry.items.flatMap(partition) }];
	});
}

/** Expose canonical validated annotations as siblings before passive cards are relocated. */
export function fencePlanLimitPauses(
	entries: TranscriptEntry[], pauses: readonly PlanLimitPauseDescriptor[],
	summaries: Record<string, PauseSummary>, context?: SourcePlacementContext, siblings = true,
): TranscriptEntry[] {
	// With nothing to relocate, keep the existing run/disclosure representation.
	if (!siblings) return attachPlanLimitPauses(entries, pauses, context).entries;
	const stubs = stubPauseTurns(entries, pauses, summaries);
	const bounded = entries.flatMap((entry) => entry.kind === 'fold' && stubs.has(entry.id)
		? splitStubFence(entry, stubs.get(entry.id)!) : [entry]);
	const attached = attachPlanLimitPauses(bounded, pauses, context).entries;
	return attached.flatMap<TranscriptEntry>((entry) => {
		if (entry.kind !== 'fold') return [entry];
		const result: TranscriptEntry[] = [];
		let from = 0;
		for (const annotation of entry.pauses ?? []) {
			const stubBound = entry.stub && annotation.pause.source.seq <= entry.stub.lastSeq ? entry.stub.lastSeq : undefined;
			const suffix = stubBound === undefined ? -1 : entry.items.findIndex((item) => (sourceRange(item.message)?.min ?? -Infinity) > stubBound);
			const end = Math.max(from, annotation.afterItem + 1, stubBound === undefined ? 0 : suffix < 0 ? entry.items.length : suffix);
			if (end > from) result.push(fragment(entry, from, end, false));
			result.push({ kind: 'pause', id: `pause:${annotation.pause.stoppedTurnId}:${annotation.pause.source.id}`, ...annotation, after_seq: stubBound });
			from = end;
		}
		if (from < entry.items.length || (!from && entry.stub) || entry.live) {
			result.push(fragment(entry, from, entry.items.length, entry.live));
		}

		return result;
	});
}

function splitStubFence(entry: TranscriptFoldEntry, turns: string[]): TranscriptEntry[] {
	const end = entry.stub!.lastSeq;
	const at = entry.items.findIndex((item) => (sourceRange(item.message)?.min ?? -Infinity) > end);
	// Only a proven numeric suffix is separable; no position is guessed for unknown rows.
	const cut = at >= 0 && entry.items.slice(at).every((item) => (sourceRange(item.message)?.min ?? -Infinity) > end)
		? at : entry.items.length;
	const prefix = { ...entry, id: `${entry.id}:stub-prefix`, items: entry.items.slice(0, cut), live: cut === entry.items.length && entry.live };
	const markers: TranscriptEntry[] = turns.map((turnId) => ({ kind: 'stub-pause', id: `stub-pause:${entry.id}:${turnId}`, turnId, after_seq: end }));
	return [prefix, ...markers, ...(cut < entry.items.length ? [runEntry(entry.items.slice(cut), entry.id, entry.live)] : [])];
}

function fragment(entry: TranscriptFoldEntry, from: number, to: number, live: boolean): TranscriptFoldEntry {
	const run = runEntry(entry.items.slice(from, to), from === 0 && to === entry.items.length ? entry.id : `${entry.id}:pause:${from}`, live);
	// A stub is indivisible and belongs to the prefix, never copied into both fragments.
	return from === 0 && entry.stub ? { ...run, stub: entry.stub, turnIds: [...new Set([...entry.stub.turnIds, ...run.turnIds])] } : run;
}
