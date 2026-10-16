import { describe, expect, it } from 'vitest';
import { contrastRatio, inkOn, parseColor } from './contrast';
import { ACCENT_ROLE_TOKENS, ACCENT_SWATCHES, accentRoles } from './projectColors';

/** Every hue at the lightnesses a project colour realistically takes, plus the offered swatches. */
const SPREAD = [
	...ACCENT_SWATCHES,
	...Array.from({ length: 36 }, (_, i) => i * 10).flatMap((hue) =>
		[35, 45, 50, 55, 62, 70].map((lightness) => `hsl(${hue}, 70%, ${lightness}%)`),
	),
];

describe('accentRoles', () => {
	it('inks the fill it paints at 4.5:1 or above in both themes across the hue spread', () => {
		for (const colour of SPREAD) {
			for (const theme of ['light', 'dark'] as const) {
				const { fill, onFill } = accentRoles(colour, theme);
				expect(contrastRatio(parseColor(fill)!, parseColor(onFill)!), `${colour} ${theme}`).toBeGreaterThanOrEqual(4.5);
			}
		}
	});

	it('fills with the colour itself unless no ink reaches 4.5:1 on it', () => {
		expect(accentRoles('#2563eb', 'light').fill).toBe('#2563eb');
		expect(accentRoles('#16a34a', 'dark').fill).toBe('#16a34a');
		// Near the crossover neither ink reaches 4.5:1 on the raw colour, so the fill steps away from its ink.
		const crossover = 'hsl(220, 50%, 54%)';
		const raw = parseColor(crossover)!;
		expect(Math.max(contrastRatio(raw, [255, 255, 255]), contrastRatio(raw, [10, 10, 11]))).toBeLessThan(4.5);
		const { fill, onFill } = accentRoles(crossover, 'light');
		expect(onFill).toBe(inkOn(crossover));
		expect(fill).not.toBe(crossover);
		expect(contrastRatio(parseColor(fill)!, parseColor(onFill)!)).toBeGreaterThanOrEqual(4.5);
	});

	it('derives the wash and hairline from the colour itself', () => {
		expect(accentRoles('#2563eb', 'dark')).toMatchObject({
			base: '#2563eb',
			wash: 'color-mix(in srgb, #2563eb 12%, transparent)',
			line: 'color-mix(in srgb, #2563eb 30%, transparent)',
		});
	});

	it('names one token per role', () => {
		expect(Object.keys(accentRoles('#2563eb', 'light')).sort()).toEqual(Object.keys(ACCENT_ROLE_TOKENS).sort());
		expect(new Set(Object.values(ACCENT_ROLE_TOKENS)).size).toBe(Object.keys(ACCENT_ROLE_TOKENS).length);
	});

	it('keeps CORSIA blue readable with each default theme ink', () => {
		expect(contrastRatio(parseColor('#2563eb')!, parseColor('#ffffff')!)).toBeCloseTo(5.17, 2);
		expect(contrastRatio(parseColor('#60a5fa')!, parseColor('#0a0a0b')!)).toBeCloseTo(7.78, 2);
	});
});
