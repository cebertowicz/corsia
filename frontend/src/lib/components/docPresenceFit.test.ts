import { describe, expect, it } from 'vitest';
import { presenceMaximum } from './docPresenceFit';

const fit = (budget: number, singleOverlaps = false) => presenceMaximum({
	budget, fullStackWidth: 90, singleWidth: 55, countWidth: 25,
	fullTitleWidth: 150, floorTitleWidth: 55, singleOverlaps,
});

describe('document header presence fit', () => {
	it('prefers three when the whole name fits, including at the boundary', () => {
		expect(fit(240)).toBe(3);
		expect(fit(239)).toBe(0);
	});
	it('prefers the whole name with the count over a cut name with an avatar', () => {
		expect(fit(175)).toBe(0);
		expect(fit(174)).toBe(1);
	});
	it('shows one avatar only when the floor fits and does not overlap', () => {
		expect(fit(110)).toBe(1);
		expect(fit(109)).toBe(0);
		expect(fit(110, true)).toBe(0);
	});
	it('resolves narrowing and widening without a latched state', () => {
		expect([240, 174, 109, 174, 240].map((budget) => fit(budget))).toEqual([3, 1, 0, 1, 3]);
	});
});
