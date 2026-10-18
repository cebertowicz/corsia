/**
 * Window math for the sidebar conversation list. Pure functions: the list renders only
 * the items near the viewport (plus rows that must stay mounted) and stands in for the
 * rest with spacers, so DOM size stops growing with the number of conversations.
 */

/** Windowing starts above this many conversation rows; at or below it the list renders in full. */
export const WINDOW_THRESHOLD = 150;
/** Items rendered beyond each edge of the viewport. */
export const WINDOW_OVERSCAN = 10;
/** Rows kept mounted on each side of the focused row, so Tab always lands on a mounted row. */
export const FOCUS_REACH = 3;

export type WindowItem<T> =
	| { kind: 'label'; key: string; text: string }
	| { kind: 'row'; key: string; thread: T; position: number };

export type Segment = { kind: 'item'; index: number } | { kind: 'gap'; height: number; before: number };

export function shouldWindow(rowCount: number): boolean {
	return rowCount > WINDOW_THRESHOLD;
}

/** Flatten the pinned section and the time groups into one ordered item list, numbering rows from 1. */
export function buildWindowItems<T extends { id: string }>(
	pinned: T[],
	groups: { label: string; threads: T[] }[],
): WindowItem<T>[] {
	const items: WindowItem<T>[] = [];
	let position = 0;
	if (pinned.length > 0) {
		items.push({ kind: 'label', key: 'label:Pinned', text: 'Pinned' });
		for (const thread of pinned) items.push({ kind: 'row', key: thread.id, thread, position: ++position });
	}
	for (const group of groups) {
		items.push({ kind: 'label', key: `label:${group.label}`, text: group.label });
		for (const thread of group.threads) items.push({ kind: 'row', key: thread.id, thread, position: ++position });
	}
	return items;
}

/** offsets[i] is the top of item i; offsets[n] is the total height. */
export function itemOffsets(heights: number[]): number[] {
	const offsets = new Array<number>(heights.length + 1);
	let total = 0;
	for (let i = 0; i < heights.length; i++) {
		offsets[i] = total;
		total += heights[i];
	}
	offsets[heights.length] = total;
	return offsets;
}

/** Index of the item that contains `y` (the last item when `y` is past the end), found by binary search. */
export function indexAt(offsets: number[], y: number): number {
	const n = offsets.length - 1;
	if (n <= 0) return 0;
	let lo = 0;
	let hi = n - 1;
	while (lo < hi) {
		const mid = (lo + hi + 1) >> 1;
		if (offsets[mid] <= y) lo = mid;
		else hi = mid - 1;
	}
	return lo;
}

/** Items intersecting the viewport [top, bottom) in list coordinates; last < first means an empty list. */
export function visibleRange(offsets: number[], top: number, bottom: number): { first: number; last: number } {
	const n = offsets.length - 1;
	if (n <= 0) return { first: 0, last: -1 };
	const first = indexAt(offsets, Math.max(0, top));
	const last = Math.max(first, indexAt(offsets, Math.max(0, bottom - 1)));
	return { first, last };
}

/** Sorted, de-duplicated indexes to render: visible range plus overscan, plus every kept index. */
export function mountedIndexes(
	count: number,
	range: { first: number; last: number },
	overscan: number,
	kept: Iterable<number>,
): number[] {
	const set = new Set<number>();
	if (range.last >= range.first) {
		const from = Math.max(0, range.first - overscan);
		const to = Math.min(count - 1, range.last + overscan);
		for (let i = from; i <= to; i++) set.add(i);
	}
	for (const i of kept) if (i >= 0 && i < count) set.add(i);
	return [...set].sort((a, b) => a - b);
}

/** Interleave mounted items with gap spacers that stand in for the unmounted runs between them. */
export function segmentsFor(offsets: number[], mounted: number[]): Segment[] {
	const n = offsets.length - 1;
	const segments: Segment[] = [];
	let next = 0;
	for (const index of mounted) {
		if (index > next) segments.push({ kind: 'gap', height: offsets[index] - offsets[next], before: index });
		segments.push({ kind: 'item', index });
		next = index + 1;
	}
	if (next < n) segments.push({ kind: 'gap', height: offsets[n] - offsets[next], before: n });
	return segments;
}

/** Indexes within `reach` of the item at `index`, for keeping the focused row's neighbours mounted. */
export function reachAround(index: number, reach: number): number[] {
	if (index < 0) return [];
	const out: number[] = [];
	for (let i = index - reach; i <= index + reach; i++) if (i >= 0) out.push(i);
	return out;
}
