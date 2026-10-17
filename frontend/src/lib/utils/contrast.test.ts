import { describe, expect, it } from 'vitest';
import { apcaLc, apcaY, contrastRatio, inkOn, parseColor, stepToContrast } from './contrast';

/**
 * `inkOn` decides the ink on every accent fill (the user bubble, primary buttons, send), so it is decided with
 * APCA, the WCAG 3 draft model, not with WCAG 2's ratio: on a mid blue the two disagree, and WCAG 2 loses.
 */

const WHITE: [number, number, number] = [255, 255, 255];
const NEAR_BLACK: [number, number, number] = [10, 10, 11];
const on = (fill: string) => Math.abs(apcaLc(WHITE, parseColor(fill)!));
const under = (fill: string) => Math.abs(apcaLc(NEAR_BLACK, parseColor(fill)!));

describe('apcaLc', () => {
	// Independent vectors: the published expected Lc of the W3 reference implementation (SAPC-8 0.0.98G-4g).
	// https://github.com/Myndex/SAPC-APCA — text first, background second, because APCA is polarity sensitive.
	it.each([
		['#888', '#fff', 63.05647],
		['#fff', '#888', -68.54146],
		['#000', '#aaa', 58.14626],
		['#aaa', '#000', -56.24113],
		['#123', '#def', 91.66831],
		['#def', '#123', -93.0677],
		['#123', '#444', 8.32326],
		['#444', '#123', -7.52688],
	])('%s on %s is Lc %s', (text, background, expected) => {
		expect(apcaLc(parseColor(text)!, parseColor(background)!)).toBeCloseTo(expected, 4);
	});

	it('signs light ink on a dark fill negative and dark ink on a light fill positive', () => {
		expect(apcaLc(WHITE, [0, 0, 0])).toBeLessThan(0);
		expect(apcaLc(NEAR_BLACK, WHITE)).toBeGreaterThan(0);
		// The reference's stated range: less than ±110, so a "body text" 75 and a "large text" 60 mean something.
		expect(Math.abs(apcaLc(WHITE, [0, 0, 0]))).toBeLessThan(110);
	});

	it('returns 0 for a colour on itself and near-0 across the loClip rollout', () => {
		expect(apcaLc([0, 131, 175], [0, 131, 175])).toBe(0);
		expect(apcaLc(parseColor('#0083af')!, parseColor('#0183af')!)).toBeCloseTo(0, 0);
	});

	it('linearises with its own 2.4 curve, not WCAG 2\'s piecewise one', () => {
		expect(apcaY([0, 131, 175])).toBeCloseTo(0.173835, 5);
		expect(apcaY([255, 255, 255])).toBeCloseTo(1, 6);
		expect(apcaY([0, 0, 0])).toBe(0);
	});
});

describe('inkOn', () => {
	it.each([
		// The default accent: WCAG 2 prefers black here (4.87 vs 4.32) and reads badly; APCA says white 74.7 vs 34.6.
		['#0083af', '#ffffff'],
		['#2563eb', '#ffffff'],
		['#facc15', '#0a0a0b'],
		// The offered swatch nearest the crossover the other way: white 60.8 vs black 48.3.
		['#ca8a04', '#ffffff'],
		['#0a0a0b', '#ffffff'],
		['#ffffff', '#0a0a0b'],
		['rgba(0, 131, 175, 0.9)', '#ffffff'],
		['not a colour', '#ffffff'],
	])('%s takes %s', (fill, ink) => {
		expect(inkOn(fill)).toBe(ink);
	});

	it('always takes the ink with the greater |Lc| on the fill', () => {
		for (let hue = 0; hue < 360; hue += 5) {
			for (const lightness of [30, 40, 50, 55, 60, 70, 80]) {
				const fill = `hsl(${hue}, 70%, ${lightness}%)`;
				const white = on(fill);
				const black = under(fill);
				expect(inkOn(fill), `${fill} (white ${white.toFixed(1)}, black ${black.toFixed(1)})`).toBe(
					white >= black ? '#ffffff' : '#0a0a0b',
				);
			}
		}
	});

	it('never leaves an accent fill below Lc 50 whichever ink it picks', () => {
		// The fill is never stepped, so this — not WCAG 2's 4.5:1 — is the readability floor of an accent fill.
		// The worst case over the hues and lightnesses a project takes is the ink crossover at ~54.8.
		for (let hue = 0; hue < 360; hue += 5) {
			for (const lightness of [30, 40, 50, 55, 60, 70, 80]) {
				const fill = `hsl(${hue}, 70%, ${lightness}%)`;
				const ink = parseColor(inkOn(fill))!;
				const lc = Math.abs(apcaLc(ink, parseColor(fill)!));
				expect(lc, `${fill} with ${inkOn(fill)}`).toBeGreaterThanOrEqual(50);
			}
		}
	});

	it('disagrees with the WCAG 2 ratio exactly where WCAG 2 is wrong', () => {
		const fill = parseColor('#0083af')!;
		// WCAG 2 says black; APCA says white; the bubble was white before the ratio shipped.
		expect(contrastRatio(fill, NEAR_BLACK)).toBeGreaterThan(contrastRatio(fill, WHITE));
		expect(inkOn('#0083af')).toBe('#ffffff');
	});
});

describe('stepToContrast', () => {
	it('keeps a colour that already reads and steps one that does not', () => {
		expect(stepToContrast([0, 131, 175], [[255, 255, 255]], 'light', 4.5)).not.toEqual([0, 131, 175]);
		expect(stepToContrast([0, 0, 0], [[255, 255, 255]], 'light', 4.5)).toEqual([0, 0, 0]);
	});
});
