import type { ChangeDesc, ChangeSet, EditorState } from '@codemirror/state';

/** Text an agent inserted that is not typed yet: [from, to) is hidden, [start, from) is typed. */
export interface Segment {
	start: number;
	from: number;
	to: number;
}

/** Carry hidden text through a change; an insertion strictly inside splits it and shows at once. */
export function mapSegments(segments: readonly Segment[], changes: ChangeDesc): Segment[] {
	const result: Segment[] = [];
	for (const segment of segments) {
		let cursor = segment.from;
		const pieces: Array<[number, number]> = [];
		changes.iterChangedRanges((fromA, toA) => {
			if (toA <= segment.from || fromA >= segment.to) return;
			if (fromA > cursor) pieces.push([cursor, fromA]);
			cursor = Math.max(cursor, toA);
		});
		if (cursor < segment.to) pieces.push([cursor, segment.to]);
		pieces.forEach(([from, to], index) => {
			const mappedFrom = changes.mapPos(from, 1);
			const mappedTo = changes.mapPos(to, -1);
			if (mappedTo > mappedFrom) {
				result.push({ start: index === 0 ? Math.min(changes.mapPos(segment.start, 1), mappedFrom) : mappedFrom, from: mappedFrom, to: mappedTo });
			}
		});
	}
	return result;
}

/** Sorted, with overlapping or touching segments joined. */
export function joinSegments(segments: readonly Segment[]): Segment[] {
	const sorted = [...segments].sort((a, b) => a.from - b.from);
	const joined: Segment[] = [];
	for (const segment of sorted) {
		const last = joined[joined.length - 1];
		if (last && segment.from <= last.to) {
			last.to = Math.max(last.to, segment.to);
			last.start = Math.min(last.start, segment.start);
		} else joined.push({ ...segment });
	}
	return joined;
}

/** Show the first `count` hidden characters, in document order; returns the new typing edge. */
export function revealFront(segments: readonly Segment[], count: number): { segments: Segment[]; edge: number | null } {
	let left = count;
	let edge: number | null = null;
	const rest: Segment[] = [];
	for (const segment of segments) {
		if (left <= 0) { rest.push(segment); continue; }
		const take = Math.min(left, segment.to - segment.from);
		left -= take;
		edge = segment.from + take;
		if (take < segment.to - segment.from) rest.push({ ...segment, from: edge });
	}
	return { segments: rest, edge };
}

/** Show every hidden character that sits before `position`. */
export function revealBefore(segments: readonly Segment[], position: number): Segment[] {
	return segments.flatMap((segment) => {
		if (segment.to <= position) return [];
		return segment.from < position ? [{ ...segment, from: position }] : [segment];
	});
}

export function hiddenCount(segments: readonly Segment[]): number {
	return segments.reduce((sum, segment) => sum + segment.to - segment.from, 0);
}

export function hiddenText(state: EditorState, segments: readonly Segment[]): string {
	return segments.map((segment) => state.sliceDoc(segment.from, segment.to)).join('');
}

/** A change or selection at or inside the span, boundaries included, is the user touching it. */
export function overlapsSpan(segment: Segment, from: number, to: number): boolean {
	return from <= segment.to && to >= segment.start;
}

export function insertedRanges(changes: ChangeSet): Array<[number, number]> {
	const ranges: Array<[number, number]> = [];
	changes.iterChangedRanges((_fromA, _toA, fromB, toB) => { if (toB > fromB) ranges.push([fromB, toB]); });
	return ranges;
}
