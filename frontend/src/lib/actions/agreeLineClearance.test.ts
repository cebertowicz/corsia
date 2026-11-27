import { describe, expect, it } from 'vitest';
import { agreeLineShift } from './agreeLineClearance';

const rect = (top: number, bottom: number) => ({ top, bottom }) as DOMRect;

describe('agreeLineShift', () => {
	it('reveals the clipped line by the least amount when the reply still fits', () => {
		expect(agreeLineShift(100, 300, rect(92, 110), rect(200, 240), rect(245, 270))).toBe(-8);
	});
	it('hides the clipped line when showing it would clip the recipient', () => {
		expect(agreeLineShift(100, 300, rect(92, 110), rect(250, 275), rect(280, 298))).toBe(10);
	});
	it('does not move a line that is already whole or hidden', () => {
		expect(agreeLineShift(100, 300, rect(100, 118), rect(200, 240), rect(245, 270))).toBe(0);
		expect(agreeLineShift(100, 300, rect(80, 100), rect(200, 240), rect(245, 270))).toBe(0);
	});
});
