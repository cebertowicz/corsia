import type { UpdateCard, VisitDivider } from '$lib/api/threadUpdates';
import type { RunStub } from '$lib/focusRuns';
import { sourceMembers, sourceRange } from './contextEventModel';
import { partitionMessageAtCards } from './messageListModel';
import { partitionAtVisit, fencePlanLimitPauses } from './transcriptFences';
import { asksYou, gatherReadySteps, isBlockLine, isPassive, placePassive } from './transcriptPassive';
import type { PlanLimitPauseDescriptor } from './planLimitPause';
import type { SourcePlacementContext } from './contextEventModel';
import {
	runEntry,
	type BlockDivider,
	type TranscriptEntry,
	type TranscriptFoldEntry,
	type TranscriptMessageEntry,
	type TranscriptUpdateEntry,
} from './transcriptFocusModel';

type SeqRange = { min: number; max: number };

/** The transcript positions an entry covers; a message not yet sequenced is at the live edge. */
function seqRange(entry: TranscriptEntry): SeqRange | null {
	if (entry.kind === 'message') {
		const range = sourceRange(entry.message);
		if (range) return range;
		// Only an actual unsequenced singleton retains the existing live-edge convention.
		const members = sourceMembers(entry.message);
		return members.length === 1 && members[0].seq == null
			? { min: Infinity, max: Infinity } : null;
	}
	if (entry.kind === 'fold') {
		const ranges = entry.items.map(seqRange);
		if (entry.stub) ranges.push({ min: entry.stub.firstSeq, max: entry.stub.lastSeq });
		if (!ranges.length || ranges.some((range) => range === null)) return null;
		return { min: Math.min(...ranges.map((range) => range!.min)), max: Math.max(...ranges.map((range) => range!.max)) };
	}
	return null;
}

/** `run` carrying `stub`, its Turns first. */
function withStub(run: TranscriptFoldEntry, stub: RunStub | undefined): TranscriptFoldEntry {
	if (!stub) return run;
	return { ...run, stub, turnIds: [...new Set([...stub.turnIds, ...run.turnIds])] };
}

function updateEntry(card: UpdateCard, earlier = 0): TranscriptUpdateEntry {
	return { kind: 'update', id: `update-${card.id}`, card, earlier };
}


/** Place each card after the last entry at or before its position; never inside a fold. */
function placeCards(entries: TranscriptEntry[], cards: UpdateCard[]): TranscriptEntry[] {
	const sorted = [...cards].sort((a, b) => a.after_seq - b.after_seq || a.id - b.id);
	const placed: TranscriptEntry[] = [];
	let next = 0;
	const cuts = [...new Set(sorted.map((card) => card.after_seq))];
	const partitioned = entries.flatMap<TranscriptEntry>((entry): TranscriptEntry[] => (
		entry.kind === 'message' ? partitionItem(entry, cuts) : [entry]
	));
	for (const entry of partitioned) {
		const range = seqRange(entry);
		while (range && next < sorted.length && sorted[next].after_seq < range.min) {
			placed.push(updateEntry(sorted[next++]));
		}
		placed.push(entry);
	}
	while (next < sorted.length) placed.push(updateEntry(sorted[next++]));
	return placed;
}

/** One consecutive run: the latest card per item, counting its earlier ones. */
function groupRun(run: TranscriptUpdateEntry[]): TranscriptUpdateEntry[] {
	const latest = new Map<string, number>();
	run.forEach((entry, index) => {
		if (entry.card.node_id) latest.set(entry.card.node_id, index);
	});
	const kept: TranscriptUpdateEntry[] = [];
	const counts = new Map<string, number>();
	run.forEach((entry, index) => {
		const nodeId = entry.card.node_id;
		const earlier = nodeId ? counts.get(nodeId) ?? 0 : 0;
		if (nodeId) counts.set(nodeId, earlier + 1);
		if (nodeId && latest.get(nodeId) !== index) return;
		kept.push(earlier ? { ...entry, earlier } : entry);
	});
	return gatherReadySteps(kept);
}

function partitionItem(item: TranscriptMessageEntry, cuts: readonly number[]): TranscriptMessageEntry[] {
	return partitionMessageAtCards(item.message, cuts).map((message) => message === item.message
		? item : { ...item, id: `${item.showContent ? 'message' : 'work'}-${message.id}`, message });
}

/**
 * One stretch between two shown messages: its runs, with the cards recorded in
 * it that ask something of you. The cards group first (`groupRun`), then each
 * closes a run at its own position.
 */
function closeRunsInGap(
	runs: TranscriptFoldEntry[],
	group: TranscriptUpdateEntry[],
): TranscriptEntry[] {
	if (group.length === 0) return runs;
	const shown = groupRun(group);
	const placed: TranscriptEntry[] = [];
	let next = 0;
	const draw = (part: TranscriptMessageEntry[], stub?: RunStub) => {
		const card = shown[next];
		if (part.length || stub) placed.push(withStub(runEntry(part, `run:${card.id}`), stub));
		placed.push(card);
		next += 1;
	};
	const cuts = [...new Set(shown.map((entry) => entry.card.after_seq))];
	runs.forEach((run, index) => {
		const items = run.items.flatMap((item) => partitionItem(item, cuts));
		let rest = items;
		// A card cannot split the unloaded rows of a stub: one inside its range follows the stub.
		let stub = run.stub;
		while (next < shown.length) {
			const after = shown[next].card.after_seq;
			const beforeStub = stub !== undefined && after < stub.firstSeq;
			const cut = beforeStub ? 0 : rest.findIndex((item) => {
				const range = seqRange(item);
				return range !== null && range.min > after;
			});
			// The card follows this run's rows: a later run of the gap may hold its place.
			if (cut === -1 && index < runs.length - 1) break;
			draw(cut === -1 ? rest : rest.slice(0, cut), beforeStub ? undefined : stub);
			if (!beforeStub) {
				stub = undefined;
				rest = cut === -1 ? [] : rest.slice(cut);
			}
		}
		if (rest === items && stub === run.stub) placed.push({ ...run, items });
		else if (rest.length || run.live || stub) placed.push(withStub(runEntry(rest, run.id, run.live), stub));
	});
	while (next < shown.length) draw([]);
	return placed;
}

/** Focus: a card that asks you something is said to you, so it closes the run it was recorded in. */
function closeRunsAtCards(entries: TranscriptEntry[], cards: UpdateCard[]): TranscriptEntry[] {
	const sorted = cards.filter(asksYou).sort((a, b) => a.after_seq - b.after_seq || a.id - b.id);
	const placed: TranscriptEntry[] = [];
	let next = 0;
	let runs: TranscriptFoldEntry[] = [];
	const closeGap = (before: number) => {
		const group: TranscriptUpdateEntry[] = [];
		while (next < sorted.length && sorted[next].after_seq < before) group.push(updateEntry(sorted[next++]));
		placed.push(...closeRunsInGap(runs, group));
		runs = [];
	};
	for (const entry of entries) {
		if (entry.kind === 'fold') {
			runs.push(entry);
			continue;
		}
		closeGap(seqRange(entry)?.min ?? -Infinity);
		placed.push(entry);
	}
	closeGap(Infinity);
	return placed;
}

function isNew(entry: TranscriptEntry, divider: VisitDivider): boolean {
	if (entry.kind === 'update') return entry.card.created_at > divider.since;
	if (entry.kind === 'passive') return entry.cards.some((card) => card.created_at > divider.since);
	const range = seqRange(entry);
	return divider.first_seq !== null && range !== null && range.max >= divider.first_seq;
}

/** F2: the visit boundary falls inside this block, so the block stays whole and carries it. */
function straddlesVisit(entry: TranscriptEntry, divider: VisitDivider): boolean {
	const range = entry.kind === 'fold' ? seqRange(entry) : null;
	return divider.first_seq !== null && range !== null
		&& range.min < divider.first_seq && range.max >= divider.first_seq;
}

/** Where an entry starts among the transcript's positions, for placing a card line in its block. */
function startOf(entry: TranscriptEntry): number | null {
	if (entry.kind === 'fold') {
		const starts = entry.items.map((item) => sourceRange(item.message)?.min ?? Infinity);
		if (entry.stub) starts.push(entry.stub.firstSeq);
		if (!starts.length && entry.cards?.length) starts.push(Math.min(...entry.cards.map((card) => card.after_seq)));
		return starts.length ? Math.min(...starts) : null;
	}
	if (entry.kind === 'message' || entry.kind === 'update') return seqRange(entry)?.min ?? null;
	return null;
}

/** The latest card per plan item; a repeat of the same item only counts as earlier. */
function latestPerItem(cards: readonly UpdateCard[]): UpdateCard[] {
	const latest = new Map<string, UpdateCard>();
	for (const card of cards) if (card.node_id) latest.set(card.node_id, card);
	return cards.filter((card) => !card.node_id || latest.get(card.node_id) === card);
}

/**
 * B6, B22: cards that ask nothing of you become lines inside the block they were recorded in,
 * at their position. A card recorded between two blocks joins the block that follows it; one
 * recorded where no block stands makes a block of its own, so it never becomes a row between blocks.
 * Returns the new list and the block each card landed in.
 */
function attachCardLines(
	entries: TranscriptEntry[],
	lines: readonly UpdateCard[],
): { entries: TranscriptEntry[]; host: Map<number, TranscriptFoldEntry> } {
	const result = [...entries];
	const owner = new Map<number, number>();
	for (const card of [...lines].sort((a, b) => a.after_seq - b.after_seq || a.id - b.id)) {
		const slots = result.flatMap((entry, index) => (startOf(entry) === null ? [] : [index]));
		const before = slots.filter((index) => startOf(result[index])! <= card.after_seq).pop();
		const after = slots.find((index) => startOf(result[index])! > card.after_seq);
		let at = -1;
		if (before !== undefined && result[before].kind === 'fold') at = before;
		else if (after !== undefined && result[after].kind === 'fold') at = after;
		if (at < 0) {
			at = before === undefined ? (after ?? result.length) : before + 1;
			result.splice(at, 0, runEntry([], `run:card-${card.id}`));
			for (const [id, index] of owner) if (index >= at) owner.set(id, index + 1);
		}
		const run = result[at] as TranscriptFoldEntry;
		result[at] = { ...run, cards: [...run.cards ?? [], card] };
		owner.set(card.id, at);
	}
	// A repeat of the same plan item inside one block reads once, at its latest.
	for (const [index, entry] of result.entries()) {
		if (entry.kind === 'fold' && entry.cards) result[index] = { ...entry, cards: latestPerItem(entry.cards) };
	}
	return { entries: result, host: new Map([...owner].map(([id, index]) => [id, result[index] as TranscriptFoldEntry])) };
}

function divided(run: TranscriptFoldEntry, divider: BlockDivider): TranscriptFoldEntry {
	return { ...run, new: true, divider };
}

/** Visit and canonical pause fences precede passive relocation. Raw source positions stay intact. */
export function mergeTranscriptUpdates(
	entries: TranscriptEntry[],
	cards: UpdateCard[],
	options: {
		focusMode: boolean; divider: VisitDivider | null;
		pauses?: readonly PlanLimitPauseDescriptor[];
		turnSummaries?: Record<string, { status?: string; cause?: string | null }>;
		sourceContext?: SourcePlacementContext;
	},
): TranscriptEntry[] {
	const { divider } = options;
	const partitioned = partitionAtVisit(entries, divider);
	if (!options.focusMode) {
		const shaped = placeCards(partitioned, cards);
		const at = divider ? shaped.findIndex((entry) => isNew(entry, divider)) : -1;
		return at < 0 ? shaped : [...shaped.slice(0, at), visitEntry(divider!), ...shaped.slice(at)];
	}
	const all = [...cards].sort((a, b) => a.after_seq - b.after_seq || a.id - b.id);
	const lines = all.filter(isBlockLine);
	const sorted = all.filter((card) => !isBlockLine(card));
	const firstNew = divider ? partitioned.findIndex((entry) => isNew(entry, divider)) : -1;
	const newCards = divider ? all.filter((card) => card.created_at > divider.since) : [];
	const hasNew = divider && (firstNew >= 0 || newCards.length > 0);
	// F2b: the first new row is inside a block, which stays whole and carries the divider.
	const inBlock = Boolean(divider && firstNew >= 0 && straddlesVisit(partitioned[firstNew], divider));
	// F3: only cards are new and the first of them is a line inside a block.
	const firstNewCard = firstNew < 0 ? newCards[0] : undefined;
	const cardInBlock = firstNewCard !== undefined && isBlockLine(firstNewCard) ? firstNewCard : undefined;
	const together = inBlock || cardInBlock !== undefined;
	const at = firstNew < 0 ? partitioned.length : firstNew;
	const split = Boolean(hasNew) && !together;
	const oldCards = split ? sorted.filter((card) => card.created_at <= divider!.since) : sorted;
	const freshCards = split ? sorted.filter((card) => card.created_at > divider!.since) : [];
	const structural = split
		? [...closeRunsAtCards(partitioned.slice(0, at), oldCards), visitEntry(divider!), ...closeRunsAtCards(partitioned.slice(at), freshCards)]
		: closeRunsAtCards(partitioned, sorted);
	// Only receipts retained as visible update rows replace their corresponding outcome.
	// The card window and grouping can omit a receipt, in which case the outcome stays.
	const receipts = new Map(structural.flatMap((entry) => entry.kind === 'update'
		&& entry.card.kind === 'person_step' && entry.card.node_id
		&& (entry.card.step_state === 'done' || entry.card.step_state === 'closed')
		? [[entry.card.node_id, entry.card.step_state] as const] : []));
	const passive = (card: UpdateCard) => isPassive(card) && !(card.node_id && (
		(receipts.get(card.node_id) === 'done' && (card.kind === 'done' || card.kind === 'done_follow_up'))
		|| (receipts.get(card.node_id) === 'closed' && card.kind === 'closed')
	));
	const fenced = fencePlanLimitPauses(structural, options.pauses ?? [], options.turnSummaries ?? {}, options.sourceContext, sorted.some(passive));
	const boundary = fenced.findIndex((entry) => entry.kind === 'divider');
	const placed = boundary < 0 ? placePassive(fenced, sorted.filter(passive)) : [
		...placePassive(fenced.slice(0, boundary), oldCards.filter(passive)), fenced[boundary],
		...placePassive(fenced.slice(boundary + 1), freshCards.filter(passive)),
	];
	const lined = lines.length ? attachCardLines(placed, lines) : { entries: placed, host: new Map<number, TranscriptFoldEntry>() };
	if (!divider || !together) return lined.entries;
	const dividerFor = (extra: Partial<BlockDivider> = {}): BlockDivider => ({
		truncated: divider.truncated === true, first_seq: divider.first_seq, ...extra,
	});
	if (cardInBlock !== undefined) {
		const run = lined.host.get(cardInBlock.id);
		return lined.entries.map((entry) => (entry === run ? divided(run, dividerFor({ beforeCardId: cardInBlock.id })) : entry));
	}
	const mark = lined.entries.findIndex((entry) => straddlesVisit(entry, divider));
	if (mark >= 0) {
		return lined.entries.map((entry, index) => (index === mark ? divided(entry as TranscriptFoldEntry, dividerFor()) : entry));
	}
	// A card closed the block exactly at the visit: nothing straddles, so the line goes between them.
	const first = lined.entries.findIndex((entry) => isNew(entry, divider));
	return first < 0 ? lined.entries : [...lined.entries.slice(0, first), visitEntry(divider), ...lined.entries.slice(first)];
}

function visitEntry(divider: VisitDivider): TranscriptEntry {
	return { kind: 'divider', id: 'visit-divider', truncated: divider.truncated === true, first_seq: divider.first_seq };
}
