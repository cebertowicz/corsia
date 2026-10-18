import { describe, expect, it } from 'vitest';
import {
	WINDOW_OVERSCAN,
	WINDOW_THRESHOLD,
	buildWindowItems,
	indexAt,
	itemOffsets,
	mountedIndexes,
	reachAround,
	segmentsFor,
	shouldWindow,
	visibleRange,
} from './sidebarWindow';

const threads = (prefix: string, n: number) => Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }));

describe('shouldWindow', () => {
	it('windows only above the threshold', () => {
		expect(shouldWindow(WINDOW_THRESHOLD)).toBe(false);
		expect(shouldWindow(WINDOW_THRESHOLD + 1)).toBe(true);
		expect(shouldWindow(0)).toBe(false);
	});
});

describe('buildWindowItems', () => {
	it('lists the pinned section, then each time group, numbering rows from 1', () => {
		const items = buildWindowItems(threads('p', 2), [
			{ label: 'Today', threads: threads('t', 2) },
			{ label: 'Older', threads: threads('o', 1) },
		]);
		expect(items.map((i) => i.key)).toEqual([
			'label:Pinned', 'p0', 'p1', 'label:Today', 't0', 't1', 'label:Older', 'o0',
		]);
		expect(items.filter((i) => i.kind === 'row').map((i) => (i.kind === 'row' ? i.position : 0))).toEqual([1, 2, 3, 4, 5]);
	});

	it('omits the Pinned label when nothing is pinned', () => {
		const items = buildWindowItems([], [{ label: 'Today', threads: threads('t', 1) }]);
		expect(items.map((i) => i.key)).toEqual(['label:Today', 't0']);
	});
});

describe('offsets and visible range', () => {
	const heights = [28, 40, 40, 40, 28, 40, 40];
	const offsets = itemOffsets(heights);

	it('builds prefix sums with the total last', () => {
		expect(offsets).toEqual([0, 28, 68, 108, 148, 176, 216, 256]);
	});

	it('finds the item containing a position, clamping past the end', () => {
		expect(indexAt(offsets, 0)).toBe(0);
		expect(indexAt(offsets, 27)).toBe(0);
		expect(indexAt(offsets, 28)).toBe(1);
		expect(indexAt(offsets, 147)).toBe(3);
		expect(indexAt(offsets, 10_000)).toBe(6);
	});

	it('returns the items intersecting the viewport', () => {
		expect(visibleRange(offsets, 0, 60)).toEqual({ first: 0, last: 1 });
		expect(visibleRange(offsets, 60, 150)).toEqual({ first: 1, last: 4 });
		expect(visibleRange(offsets, 148, 149)).toEqual({ first: 4, last: 4 });
	});

	it('handles an empty list and a viewport above the list', () => {
		expect(visibleRange([0], 0, 100)).toEqual({ first: 0, last: -1 });
		expect(visibleRange(offsets, -500, -100)).toEqual({ first: 0, last: 0 });
	});
});

describe('mountedIndexes', () => {
	it('adds overscan on each side, clamped to the list', () => {
		expect(mountedIndexes(100, { first: 50, last: 55 }, WINDOW_OVERSCAN, [])).toEqual(
			Array.from({ length: 6 + 2 * WINDOW_OVERSCAN }, (_, i) => 40 + i),
		);
		expect(mountedIndexes(20, { first: 0, last: 2 }, 5, [])).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
		expect(mountedIndexes(20, { first: 18, last: 19 }, 5, [])).toEqual([13, 14, 15, 16, 17, 18, 19]);
	});

	it('always includes kept rows, even far outside the window, and drops invalid ones', () => {
		expect(mountedIndexes(100, { first: 0, last: 1 }, 1, [90, 3, -1, 200])).toEqual([0, 1, 2, 3, 90]);
	});

	it('mounts nothing for an empty visible range but still keeps kept rows', () => {
		expect(mountedIndexes(5, { first: 0, last: -1 }, 10, [2])).toEqual([2]);
	});
});

describe('segmentsFor', () => {
	const offsets = itemOffsets([10, 10, 10, 10, 10, 10]);

	it('stands in for unmounted runs with gaps whose heights add up to the whole list', () => {
		const segments = segmentsFor(offsets, [2, 3]);
		expect(segments).toEqual([
			{ kind: 'gap', height: 20, before: 2 },
			{ kind: 'item', index: 2 },
			{ kind: 'item', index: 3 },
			{ kind: 'gap', height: 20, before: 6 },
		]);
	});

	it('places a gap on each side of a kept row that is not contiguous with the window', () => {
		const segments = segmentsFor(offsets, [0, 1, 4]);
		expect(segments).toEqual([
			{ kind: 'item', index: 0 },
			{ kind: 'item', index: 1 },
			{ kind: 'gap', height: 20, before: 4 },
			{ kind: 'item', index: 4 },
			{ kind: 'gap', height: 10, before: 6 },
		]);
	});

	it('emits no gaps when everything is mounted, and one gap when nothing is', () => {
		expect(segmentsFor(offsets, [0, 1, 2, 3, 4, 5]).every((s) => s.kind === 'item')).toBe(true);
		expect(segmentsFor(offsets, [])).toEqual([{ kind: 'gap', height: 60, before: 6 }]);
	});

	it('keeps total height constant as the window moves', () => {
		const heights = [28, 40, 52, 40, 28, 40, 40, 40];
		const o = itemOffsets(heights);
		for (const mounted of [[0, 1], [3, 4, 5], [6, 7], []]) {
			const total = segmentsFor(o, mounted).reduce(
				(sum, s) => sum + (s.kind === 'gap' ? s.height : heights[s.index]),
				0,
			);
			expect(total).toBe(o[o.length - 1]);
		}
	});
});

describe('reachAround', () => {
	it('lists neighbours on both sides of the focused row', () => {
		expect(reachAround(10, 3)).toEqual([7, 8, 9, 10, 11, 12, 13]);
		expect(reachAround(1, 3)).toEqual([0, 1, 2, 3, 4]);
		expect(reachAround(-1, 3)).toEqual([]);
	});
});
