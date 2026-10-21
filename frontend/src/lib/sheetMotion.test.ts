import { describe, expect, it } from 'vitest';
import { dismissDistance, releaseSheetDrag, sheetFly } from './sheetMotion';

const base = { detent: 'full' as const, released: true, raw: 0, elapsed: 500, height: 500 };

describe('sheetFly', () => {
	it('slides 72px over 260ms, or not at all under reduced motion', () => {
		expect(sheetFly(false)).toMatchObject({ y: 72, duration: 260 });
		expect(sheetFly(true)).toEqual({ y: 0, opacity: 1, duration: 0 });
		// Under reduced motion the sheet holds still and opaque for the backdrop's crossfade, so its outro ends with that fade, not before it.
		expect(sheetFly(true, 120)).toEqual({ y: 0, opacity: 1, duration: 120 });
	});
});

describe('dismissDistance', () => {
	it('is a quarter of the sheet held between 96 and 180px', () => {
		expect(dismissDistance(200)).toBe(96);
		expect(dismissDistance(500)).toBe(120);
		expect(dismissDistance(1000)).toBe(180);
	});
});

describe('releaseSheetDrag', () => {
	it('expands a half sheet on a 40px rise or a quick tap', () => {
		expect(releaseSheetDrag({ ...base, detent: 'half', raw: -40 })).toBe('expand');
		expect(releaseSheetDrag({ ...base, detent: 'half', raw: 3, elapsed: 200 })).toBe('expand');
		expect(releaseSheetDrag({ ...base, detent: 'half', raw: 3, elapsed: 400 })).toBe('settle');
	});

	it('never expands a full sheet or on a cancelled pointer', () => {
		expect(releaseSheetDrag({ ...base, raw: -60 })).toBe('settle');
		expect(releaseSheetDrag({ ...base, detent: 'half', released: false, raw: -60 })).toBe('settle');
	});

	it('dismisses past the dismiss distance', () => {
		expect(releaseSheetDrag({ ...base, raw: 119 })).toBe('settle');
		expect(releaseSheetDrag({ ...base, raw: 120 })).toBe('dismiss');
		expect(releaseSheetDrag({ ...base, detent: 'half', released: false, raw: 120 })).toBe('dismiss');
	});

	it('dismisses a fast flick past 64px', () => {
		expect(releaseSheetDrag({ ...base, raw: 70, elapsed: 80 })).toBe('dismiss');
		expect(releaseSheetDrag({ ...base, raw: 70, elapsed: 200 })).toBe('settle');
		expect(releaseSheetDrag({ ...base, raw: 60, elapsed: 20 })).toBe('settle');
	});
});
