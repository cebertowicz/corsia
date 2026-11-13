import { afterEach, describe, expect, it } from 'vitest';
import { GAP, placeCards, revealShift } from './docMarginPlacement';
import { MARGIN_MIN, hasRoomForMargin } from './docCommentSurfaceState.svelte';
import { PANE_DRAWER_WIDTH } from './artifactSplitLayout';
import { paneOverlay } from './stores/artifactCanvas';

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

	it('at rest, stacks every card under what heads the column (its first item, at the top)', () => {
		const items = [card('head', 0, 42), card('a', 20), card('b', 30), card('c', 40)];
		expect(placeCards(items, null)).toEqual({ head: 0, a: 50, b: 50 + 100 + GAP, c: 50 + 2 * (100 + GAP) });
	});

	it('never lifts a card above the column top: when the cards above a pick do not fit between the top and its line, they stack down from the top and the pick follows them', () => {
		const items = [card('head', 0, 42), card('a', 20), card('b', 30), card('c', 40)];
		const tops = placeCards(items, 'c');
		expect(tops).toEqual({ head: 0, a: 50, b: 50 + 100 + GAP, c: 50 + 2 * (100 + GAP) });
		expect(overlaps(items, tops)).toBe(false);
	});

	it('steps the cards above a pick up only as far as they must: the head stays at the top and a card with room keeps its line', () => {
		expect(placeCards([card('head', 0, 42), card('a', 300), card('b', 310)], 'b')).toEqual({ head: 0, a: 310 - GAP - 100, b: 310 });
		// A first card held under the head (its text is above the head's foot) stays there; the pick keeps its line and steps its neighbour up.
		expect(placeCards([card('head', 0, 42), card('a', 20), card('b', 400), card('c', 410)], 'c')).toEqual({ head: 0, a: 50, b: 410 - GAP - 100, c: 410 });
	});

	it('keeps a picked card at its line whenever the cards above it fit, keeps the head at the top, and puts no card above it', () => {
		const items = [card('head', 0, 42), ...Array.from({ length: 6 }, (_, i) => card(`t${i}`, 540 + 10 * i, 120))];
		for (const [i, item] of items.entries()) {
			if (i === 0) continue;
			const tops = placeCards(items, item.id);
			const above = items.slice(0, i).reduce((sum, it) => sum + it.h + GAP, 0);
			expect(tops[item.id]).toBe(Math.max(item.y, above));
			expect(tops.head).toBe(0);
			expect(Math.min(...Object.values(tops))).toBe(0);
			expect(overlaps(items, tops)).toBe(false);
		}
		// The first five picks have room above them and keep their lines; the sixth has not, and sits under the five.
		expect(items.slice(1, 6).map((item) => placeCards(items, item.id)[item.id])).toEqual(items.slice(1, 6).map((item) => item.y));
		expect(placeCards(items, 't5').t5).toBe(42 + GAP + 5 * (120 + GAP));
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

	describe('under a Workspace panel the shell shows over the canvas', () => {
		afterEach(() => paneOverlay.set(false));

		it('counts only the canvas the panel leaves uncovered', () => {
			expect(hasRoomForMargin(MARGIN_MIN + PANE_DRAWER_WIDTH, true)).toBe(true);
			expect(hasRoomForMargin(MARGIN_MIN + PANE_DRAWER_WIDTH - 1, true)).toBe(false);
			expect(hasRoomForMargin(MARGIN_MIN, true)).toBe(false);
		});

		it('reads the shell\'s answer when none is passed', () => {
			expect(hasRoomForMargin(800)).toBe(true);
			paneOverlay.set(true);
			expect(hasRoomForMargin(800)).toBe(false);
			expect(hasRoomForMargin(MARGIN_MIN + PANE_DRAWER_WIDTH)).toBe(true);
		});
	});
});

describe('revealShift: the text scrolls so a grown card shows its lower end', () => {
	const view = { bottom: 700 };
	it('is nothing when the card already ends inside the view with its gap', () => {
		expect(revealShift({ bottom: 500 }, view)).toBe(0);
		expect(revealShift({ bottom: 700 - GAP }, view)).toBe(0);
	});
	it('is how far the card reaches below the view, gap included', () => {
		expect(revealShift({ bottom: 720 }, view)).toBe(720 + GAP - 700);
	});
	it('is that far even for a card taller than the view, whose top then goes above it', () => {
		expect(revealShift({ bottom: 1900 }, view)).toBe(1900 + GAP - 700);
	});
});
