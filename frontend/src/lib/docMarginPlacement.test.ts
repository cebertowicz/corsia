import { afterEach, describe, expect, it } from 'vitest';
import { GAP, placeCards, revealShift } from './docMarginPlacement';
import { MARGIN_MIN, hasRoomForMargin, marginFits } from './docCommentSurfaceState.svelte';
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
		const root = (width: number) => ({ getBoundingClientRect: () => ({ width }) }) as HTMLElement;

		it('counts only the canvas the panel leaves uncovered', () => {
			expect(hasRoomForMargin(MARGIN_MIN + PANE_DRAWER_WIDTH, true)).toBe(true);
			expect(hasRoomForMargin(MARGIN_MIN + PANE_DRAWER_WIDTH - 1, true)).toBe(false);
			expect(hasRoomForMargin(MARGIN_MIN, true)).toBe(false);
		});

		it('reads the shell\'s answer when none is passed, so marginFits agrees with the surface', () => {
			expect(marginFits(root(800), false)).toBe(true);
			paneOverlay.set(true);
			expect(hasRoomForMargin(800)).toBe(false);
			expect(marginFits(root(800), false)).toBe(false);
			expect(marginFits(root(MARGIN_MIN + PANE_DRAWER_WIDTH), false)).toBe(true);
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
