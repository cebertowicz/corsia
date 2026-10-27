import { describe, expect, it } from 'vitest';
import { countUp } from './percentCountUp';

const FRAME_MS = 16;

describe('countUp', () => {
	it.each([1, 4, 20, 55, 100])('closes a gap of %i points in under a second without passing the target', (gap) => {
		let shown = 0;
		let elapsed = 0;
		while (shown < gap && elapsed < 2_000) {
			const next = countUp(shown, gap, FRAME_MS);
			expect(next).toBeGreaterThan(shown);
			expect(next).toBeLessThanOrEqual(gap);
			shown = next;
			elapsed += FRAME_MS;
		}
		expect(shown).toBe(gap);
		expect(elapsed).toBeLessThanOrEqual(1_000);
	});

	it('never goes back when the target is at or below the shown value', () => {
		expect(countUp(60, 60, FRAME_MS)).toBe(60);
		expect(countUp(60, 40, FRAME_MS)).toBe(60);
	});
});
