import { describe, expect, it } from 'vitest';
import { GAP, placeCards } from './docMarginPlacement';
import { MARGIN_MIN, hasRoomForMargin } from './docCommentSurfaceState.svelte';

const card = (id: string, y: number, h = 100) => ({ id, y, h });
const overlaps = (items: { id: string; h: number }[], tops: Record<string, number>) =>
	items.some((a, i) => items.slice(i + 1).some((b) => tops[a.id] < tops[b.id] + b.h && tops[b.id] < tops[a.id] + a.h));

describe('placeCards: soft-pinned margin cards', () => {
	it('puts a card beside its own text when nothing is near it', () => {
		expect(placeCards([card('a', 40), card('b', 400)], null)).toEqual({ a: 40, b: 400 });
	});

	it('stacks a cluster downward in document order with a gap, never overlapping', () => {
		const items = [card('a', 100), card('b', 110), card('c', 120)];
		const tops = placeCards(items, null);
		expect(tops).toEqual({ a: 100, b: 100 + 100 + GAP, c: 100 + 2 * (100 + GAP) });
		expect(overlaps(items, tops)).toBe(false);
	});

	it('keeps the selected card level with its text and pushes the others aside, above and below', () => {
		const items = [card('a', 100), card('b', 110), card('c', 120)];
		const tops = placeCards(items, 'b');
		expect(tops.b).toBe(110);
		expect(tops.a).toBe(110 - GAP - 100);
		expect(tops.c).toBe(110 + 100 + GAP);
		expect(overlaps(items, tops)).toBe(false);
	});

	it('leaves a card that is already clear of the selected one where its text is', () => {
		const tops = placeCards([card('a', 0), card('b', 500), card('c', 1000)], 'b');
		expect(tops).toEqual({ a: 0, b: 500, c: 1000 });
	});

	it('keeps document order whatever is selected', () => {
		const items = [card('a', 100), card('b', 110), card('c', 120), card('d', 130)];
		for (const selected of ['a', 'b', 'c', 'd', null]) {
			const tops = placeCards(items, selected);
			expect(items.map((item) => tops[item.id])).toEqual([...items.map((item) => tops[item.id])].sort((x, y) => x - y));
		}
	});

	it('gives every card a top, and an unknown selection changes nothing', () => {
		const items = [card('a', 10), card('b', 20)];
		expect(placeCards(items, 'missing')).toEqual(placeCards(items, null));
		expect(placeCards([], 'a')).toEqual({});
	});
});

describe('hasRoomForMargin: the one width rule', () => {
	it('is true from MARGIN_MIN up and false below it', () => {
		expect(hasRoomForMargin(MARGIN_MIN)).toBe(true);
		expect(hasRoomForMargin(MARGIN_MIN - 1)).toBe(false);
		expect(hasRoomForMargin(1440)).toBe(true);
		expect(hasRoomForMargin(393)).toBe(false);
	});
});
