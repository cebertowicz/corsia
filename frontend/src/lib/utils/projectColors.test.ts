import { describe, expect, it } from 'vitest';
import { contrastRatio, parseColor } from './contrast';
import { ACCENT_SWATCHES, accentRoles } from './projectColors';

describe('accentRoles', () => {
	it('keeps primary-action ink at 4.5:1 or above for every offered project accent', () => {
		for (const colour of ACCENT_SWATCHES) {
			for (const theme of ['light', 'dark'] as const) {
				const { onFill } = accentRoles(colour, theme);
				expect(contrastRatio(parseColor(colour)!, parseColor(onFill)!)).toBeGreaterThanOrEqual(4.5);
			}
		}
	});

	it('keeps CORSIA blue readable with each default theme ink', () => {
		expect(contrastRatio(parseColor('#2563eb')!, parseColor('#ffffff')!)).toBeCloseTo(5.17, 2);
		expect(contrastRatio(parseColor('#60a5fa')!, parseColor('#0a0a0b')!)).toBeCloseTo(7.78, 2);
	});
});
