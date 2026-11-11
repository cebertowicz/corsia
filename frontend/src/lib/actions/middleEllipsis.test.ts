import { describe, expect, it } from 'vitest';
import { fitPrefix } from './middleEllipsis';

// A monospace stand-in for the browser's measure: every character, "…" included, is 10 wide.
const wide = (text: string) => Array.from(text).length * 10;

describe('fitPrefix', () => {
	it('returns a start that already fits as it is, with no ellipsis', () => {
		expect(fitPrefix('drainage', wide, 80)).toBe('drainage');
		expect(fitPrefix('drainage', wide, 200)).toBe('drainage');
	});

	it('cuts to the longest start that fits with the ellipsis, leaving no spare room', () => {
		const out = fitPrefix('drainage_cut_and_bridge', wide, 100);
		expect(out).toBe('drainage_…');
		expect(wide(out)).toBe(100);
	});

	it('uses a fractional width without going over it', () => {
		const out = fitPrefix('drainage_cut_and_bridge', wide, 104.9);
		expect(out).toBe('drainage_…');
		expect(wide(out)).toBeLessThanOrEqual(104.9);
	});

	it('keeps whole characters, never half of a pair', () => {
		expect(fitPrefix('😀😀😀😀😀', wide, 30)).toBe('😀😀…');
	});

	it('falls to the bare ellipsis when not even one letter fits', () => {
		expect(fitPrefix('drainage', wide, 10)).toBe('…');
		expect(fitPrefix('drainage', wide, 5)).toBe('…');
	});
});
