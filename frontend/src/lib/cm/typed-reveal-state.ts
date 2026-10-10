import { EditorState, Facet, StateEffect, StateField, Transaction, type ChangeSet, type Range } from '@codemirror/state';
import { Decoration, EditorView, type DecorationSet } from '@codemirror/view';
import { agentColor } from '$lib/docPresence';
import { hashSeed, ghostHoldMs, mulberry32, selectSweepMs } from '$lib/typingPace';
import { currentLiveUpdateAuthor, type LiveUpdateAuthor } from '$lib/stores/liveUpdateAuthor';
import { commentRanges } from './comment-marks';
import { PresenceCursor, revealingThreads } from './presence-marks';
import { suggestionTyping } from './suggestion-marks';
import { adoptPreviewSelect, typedSuggestions, withAdopted, withClaimedSuggestions } from './typed-reveal-claims';
import {
	beginSelect, endSelect, GHOST_MAX_CHARS, nextGhostKey, selectHead, selectMarks, selectShown, selectTheme, type Selecting,
} from './typed-reveal-select';
import {
	hiddenCount, insertedRanges, joinSegments, mapSegments, overlapsSpan, revealBefore, revealFront, type Segment,
} from './typed-reveal-segments';
import {
	carriedPreviews, endPreview, handOver, previewCaret, previewMarks, previewSlot, typePreviews, withoutCall, withPreviewSlot,
	type Preview,
} from './typed-reveal-preview';

/**
 * Presentation only: every character here is already in the document. A lane hides
 * one agent not-yet-typed text and draws that agent caret; ending a lane for any
 * reason simply shows what is left.
 */
export interface Lane {
	threadId: string;
	name: string;
	color: string;
	segments: readonly Segment[];
	/** Where the caret rests once nothing is hidden. */
	caret: number | null;
	/** Bumps when text joins the backlog, so the pace is recomputed from what remains. */
	generation: number;
	typing: boolean;
	quiet: boolean;
	/** The select step running now (sweep, then hold), if any; typing waits for it. One per lane, never replaced. */
	select: Selecting | null;
	/** Edits still being generated, typed after the hidden text; never document text. */
	previews: readonly Preview[];
}

export interface TypedRevealOptions {
	/** False when typing must not start: reduced motion, hidden tab, no live connection. */
	canType: () => boolean;
	/** Whether an inserted range will be on screen once the update is drawn. */
	isInView: (changes: ChangeSet, from: number, to: number) => boolean;
}

export const typedRevealOptions = Facet.define<TypedRevealOptions, TypedRevealOptions>({
	combine: (values) => values[0] ?? { canType: () => false, isInView: () => true },
});

export const authored = StateEffect.define<LiveUpdateAuthor>();
export const revealCount = StateEffect.define<{ threadId: string; count: number }>();
export const laneState = StateEffect.define<{ threadId: string; typing: boolean; quiet: boolean }>();
export const dropLane = StateEffect.define<string>();
export const revealAll = StateEffect.define<null>();
export const revealThrough = StateEffect.define<number>();

/** Read the writer at transaction creation, while the update is still being applied. */
export const authorTagger = EditorState.transactionExtender.of((transaction) => {
	const author = transaction.docChanged ? currentLiveUpdateAuthor() : null;
	return author ? { effects: authored.of(author) } : null;
});

/**
 * HELD ENDS in one place (map rules 8 and 9): the user acting inside an unrevealed
 * span ends typing for that span. A change is read in old coordinates, a selection in new.
 */
export function userTouchedByChange(transaction: Transaction, segment: Segment): boolean {
	if (!transaction.docChanged || transaction.annotation(Transaction.userEvent) === undefined) return false;
	let touched = false;
	transaction.changes.iterChangedRanges((fromA, toA) => { if (overlapsSpan(segment, fromA, toA)) touched = true; });
	return touched;
}

export function userTouchedBySelection(transaction: Transaction, segment: Segment): boolean {
	const selection = transaction.selection;
	return !!selection && selection.ranges.some((range) => overlapsSpan(segment, range.from, range.to));
}

function cleaned(lane: Lane, segments: readonly Segment[], edge: number | null): Lane | null {
	const caret = edge ?? lane.caret;
	return segments.length || caret !== null || lane.select || lane.previews.length ? { ...lane, segments, caret } : null;
}

function carried(lane: Lane, transaction: Transaction): Lane | null {
	if (!transaction.docChanged && !transaction.selection) return lane;
	const kept = lane.segments.filter((segment) => !userTouchedByChange(transaction, segment));
	const mapped = mapSegments(kept, transaction.changes)
		.filter((segment) => !userTouchedBySelection(transaction, segment));
	const rest = lane.caret === null ? null : transaction.changes.mapPos(lane.caret, 1);
	const last = lane.segments[lane.segments.length - 1];
	const caret = mapped.length || !last ? rest : transaction.changes.mapPos(last.to, -1);
	const previews = carriedPreviews(lane.previews, transaction,
		(span) => userTouchedByChange(transaction, span) || userTouchedBySelection(transaction, span));
	return cleaned({ ...carriedSelect(lane, transaction), previews }, mapped, caret);
}

/** Carry the select step through a change, whoever made it; a comment anchor deleted outright has nothing left to select. */
function carriedSelect(lane: Lane, transaction: Transaction): Lane {
	const select = lane.select;
	if (!select) return lane;
	const from = transaction.changes.mapPos(select.from, select.ghost !== null ? -1 : 1);
	const to = Math.max(from, transaction.changes.mapPos(select.to, -1));
	return { ...lane, select: select.ghost === null && to <= from ? null : { ...select, from, to } };
}

/** The first replaced or deleted range an agent edit made, as a ghost select step. */
function ghostSelect(transaction: Transaction, author: LiveUpdateAuthor, inView: (from: number, to: number) => boolean): Selecting | null {
	let found: Selecting | null = null;
	transaction.changes.iterChangedRanges((fromA, toA, fromB, toB) => {
		if (found || toA <= fromA || toA - fromA > GHOST_MAX_CHARS || !inView(fromB, Math.max(toB, fromB + 1))) return;
		const ghost = transaction.startState.sliceDoc(fromA, toA);
		found = {
			key: nextGhostKey(), from: fromB, to: fromB, ghost, length: ghost.length, shown: 0, sweepMs: selectSweepMs(ghost.length),
			holdMs: ghostHoldMs(mulberry32(hashSeed(`${author.threadId}:${fromB}:${ghost.length}`))),
		};
	});
	return found;
}

/** An agent comment arrival starts its select on a free slot; a busy lane keeps the run it has. */
function withBegun(lanes: readonly Lane[], begun: { threadId: string; name: string; color: string; select: Selecting }): Lane[] {
	const own = lanes.find((lane) => lane.threadId === begun.threadId);
	if (own?.select) return [...lanes];
	const lane: Lane = own ? { ...own, name: begun.name, select: begun.select } : {
		threadId: begun.threadId, name: begun.name, color: begun.color,
		segments: [], caret: null, generation: 0, typing: false, quiet: false, select: begun.select, previews: [],
	};
	return [...lanes.filter((other) => other !== own), lane];
}

function withAuthored(lanes: readonly Lane[], author: LiveUpdateAuthor, transaction: Transaction, state: EditorState): Lane[] {
	const options = state.facet(typedRevealOptions);
	const inOthers = (from: number) => {
		let inside = false;
		const old = transaction.startState.field(typedReveals, false) ?? [];
		transaction.changes.iterChangedRanges((fromA, _toA, fromB) => {
			if (fromB === from && old.some((lane) => lane.threadId !== author.threadId
				&& lane.segments.some((segment) => segment.from < fromA && fromA < segment.to))) inside = true;
		});
		return inside;
	};
	let rest = [...lanes];
	const own = rest.find((lane) => lane.threadId === author.threadId);
	const { lane, claimed } = handOver(own ?? {
		threadId: author.threadId, name: author.name, color: agentColor(author.threadId),
		segments: [], caret: null, generation: 0, typing: true, quiet: false, select: null, previews: [],
	}, author, state);
	// An insert strictly inside another agent hidden span shows at once (map rule 7); a taken-over preview's region is already placed.
	const typeable = options.canType()
		? insertedRanges(transaction.changes).filter(([from, to]) => !inOthers(from) && options.isInView(transaction.changes, from, to)
			&& !claimed.some((region) => from <= region.to && to >= region.start))
		: [];
	rest = rest.filter((other) => other !== own);
	let last: number | null = null;
	transaction.changes.iterChangedRanges((_a, _b, fromB, toB) => { last = toB > fromB ? toB : fromB; });
	const ghost = options.canType() && !claimed.length
		? ghostSelect(transaction, author, (from, to) => options.isInView(transaction.changes, from, to)) : null;
	const taken = options.canType() ? claimed.filter((region) => region.to > region.from) : [];
	const added = [...typeable.map(([from, to]): Segment => ({ start: from, from, to })), ...taken];
	const next: Lane = {
		...lane, name: author.name,
		segments: joinSegments([...lane.segments, ...added]),
		caret: last ?? lane.caret,
		generation: lane.generation + (added.length ? 1 : 0),
		typing: added.length > 0, quiet: false,
		select: lane.select ?? ghost,
	};
	return [...rest, next];
}

export const typedReveals = StateField.define<readonly Lane[]>({
	create: () => [],
	update(lanes, transaction) {
		let next = lanes.length
			? lanes.flatMap((lane) => carried(lane, transaction) ?? [])
			: lanes;
		for (const effect of transaction.effects) {
			if (effect.is(authored)) next = withAuthored(next, effect.value, transaction, transaction.state);
			else if (effect.is(revealCount)) {
				const { threadId, count } = effect.value;
				next = next.flatMap((lane) => {
					if (lane.threadId !== threadId) return [lane];
					const { segments, edge } = revealFront(lane.segments, count);
					const previews = typePreviews(lane.previews, count - hiddenCount(lane.segments) + hiddenCount(segments));
					return cleaned({ ...lane, previews }, segments, edge) ?? [];
				});
			} else if (effect.is(laneState)) {
				const state = effect.value;
				next = next.map((lane) => lane.threadId === state.threadId ? { ...lane, typing: state.typing, quiet: state.quiet } : lane);
			} else if (effect.is(dropLane)) next = next.filter((lane) => lane.threadId !== effect.value);
			else if (effect.is(revealAll)) {
				next = next.flatMap((lane) => cleaned({ ...lane, select: null, previews: [] }, [], lane.segments.length ? lane.segments[lane.segments.length - 1].to : lane.caret) ?? []);
			} else if (effect.is(revealThrough)) {
				next = next.map((lane) => {
					const segments = revealBefore(lane.segments, effect.value);
					return hiddenCount(segments) === hiddenCount(lane.segments) ? lane : { ...lane, segments, caret: segments.length ? lane.caret : effect.value };
				});
			}
			else if (effect.is(beginSelect)) next = withBegun(next, effect.value);
			else if (effect.is(previewSlot)) {
				const options = transaction.state.facet(typedRevealOptions);
				if (options.canType()) next = withPreviewSlot(next, effect.value, transaction.state, (from, to) => options.isInView(transaction.changes, from, to));
			} else if (effect.is(endPreview)) {
				next = next.flatMap((lane) => { const kept = withoutCall(lane, effect.value.callId); return cleaned(kept, kept.segments, kept.caret) ?? []; });
			}
			else if (effect.is(adoptPreviewSelect)) next = withAdopted(next, effect.value);
			else if (effect.is(selectShown)) {
				const { threadId, key, shown } = effect.value;
				next = next.map((lane) => lane.threadId === threadId && lane.select?.key === key ? { ...lane, select: { ...lane.select, shown } } : lane);
			} else if (effect.is(endSelect)) {
				const { threadId, key } = effect.value;
				next = next.flatMap((lane) => lane.threadId !== threadId || lane.select?.key !== key
					? [lane] : cleaned({ ...lane, select: null }, lane.segments, lane.caret) ?? []);
			}
		}
		const claimed = withClaimedSuggestions(next, transaction.state.field(commentRanges, false) ?? []);
		return claimed.some((lane, index) => lane !== next[index])
			? claimed.flatMap((lane) => cleaned(lane, lane.segments, lane.caret) ?? []) : next;
	},
	provide: (field) => [
		EditorView.decorations.compute([field, commentRanges], (state) => laneDecorations(state.field(field),
			new Set(state.field(commentRanges).flatMap((range) => range.suggestion ? [range.id] : [])))),
		suggestionTyping.compute([field], (state) => typedSuggestions(state.field(field))),
		selectTheme,
		revealingThreads.compute([field], (state) => new Set(state.field(field).map((lane) => lane.threadId))),
	],
});

function laneDecorations(lanes: readonly Lane[], committed: ReadonlySet<string>): DecorationSet {
	return Decoration.set(lanes.flatMap((lane) => {
		const front = lane.segments[0];
		const preview = lane.select || front ? null : previewCaret(lane);
		const at = lane.select ? selectHead(lane.select) : front ? front.from : preview?.at ?? lane.caret;
		const marks: Array<Range<Decoration>> = at === null ? [] : [Decoration.widget({
			widget: new PresenceCursor(lane.threadId, lane.name, lane.color, lane.typing || !!lane.select, lane.quiet),
			side: preview?.after ? 2 : front || lane.select || lane.previews.length ? -1 : 1,
		}).range(at)];
		if (lane.select) marks.push(...selectMarks(lane.select, lane.color, lane.threadId));
		marks.push(...previewMarks(lane, committed));
		for (const segment of lane.segments) {
			if (segment.from > segment.start) {
				marks.push(Decoration.mark({
					class: 'cm-presence-arrival',
					attributes: { style: `--presence-color:${lane.color}`, 'data-thread-id': lane.threadId },
				}).range(segment.start, segment.from));
			}
			marks.push(Decoration.replace({}).range(segment.from, segment.to));
		}
		return marks;
	}), true);
}
