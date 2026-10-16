import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apcaLc, contrastRatio, inkOn, parseColor, parseCssColour } from './contrast';
import { ACCENT_ROLE_TOKENS, ACCENT_SWATCHES, ACCENT_TEXT_SURFACES, accentRoles, FILL_INK_MIN_LC } from './projectColors';

/** Every hue at the lightnesses a project colour realistically takes, plus the offered swatches. */
const SPREAD = [
	...ACCENT_SWATCHES,
	...Array.from({ length: 36 }, (_, i) => i * 10).flatMap((hue) =>
		[35, 45, 50, 55, 62, 70].map((lightness) => `hsl(${hue}, 70%, ${lightness}%)`),
	),
];

const THEMES = ['light', 'dark'] as const;

/** Text reads on every pane, and the ink reads on the fill, in both themes. */
function expectReadable(colour: string) {
	for (const theme of THEMES) {
		const roles = accentRoles(colour, theme);
		expect(roles, `${colour} ${theme}`).not.toBeNull();
		const { text, fill, onFill } = roles!;
		for (const surface of ACCENT_TEXT_SURFACES[theme]) {
			expect(contrastRatio(parseColor(text)!, parseColor(surface)!), `${colour} ${theme} text on ${surface}`).toBeGreaterThanOrEqual(4.5);
		}
		expect(Math.abs(apcaLc(parseColor(onFill)!, parseColor(fill)!)), `${colour} ${theme} ink on fill`).toBeGreaterThanOrEqual(FILL_INK_MIN_LC);
	}
}

describe('accentRoles', () => {
	it('fills with the colour itself, for every swatch and in both themes', () => {
		for (const colour of ACCENT_SWATCHES) {
			for (const theme of THEMES) {
				expect(accentRoles(colour, theme)!.fill, `${colour} ${theme}`).toBe(colour);
			}
		}
		expect(accentRoles('#0083af', 'light')!.fill).toBe('#0083af');
		expect(accentRoles('#0083af', 'dark')!.fill).toBe('#0083af');
	});

	it('inks the fill it paints at the APCA floor or above in both themes across the hue spread', () => {
		for (const colour of SPREAD) {
			for (const theme of THEMES) {
				const { fill, onFill } = accentRoles(colour, theme)!;
				const lc = Math.abs(apcaLc(parseColor(onFill)!, parseColor(fill)!));
				expect(lc, `${colour} ${theme} ${onFill} on ${fill}`).toBeGreaterThanOrEqual(FILL_INK_MIN_LC);
			}
		}
	});

	it('steps the fill away from its ink only where neither ink reaches the APCA floor on it', () => {
		// APCA's crossover sits lighter than WCAG 2's: here white and near-black both read below the floor.
		const crossover = 'hsl(220, 50%, 68%)';
		const raw = parseColor(crossover)!;
		expect(Math.max(Math.abs(apcaLc([255, 255, 255], raw)), Math.abs(apcaLc([10, 10, 11], raw)))).toBeLessThan(FILL_INK_MIN_LC);
		for (const theme of THEMES) {
			const { fill, onFill } = accentRoles(crossover, theme)!;
			expect(onFill).toBe(inkOn(crossover));
			expect(fill).not.toBe(crossover);
			expect(Math.abs(apcaLc(parseColor(onFill)!, parseColor(fill)!))).toBeGreaterThanOrEqual(FILL_INK_MIN_LC);
		}
	});

	it('derives the wash and hairline from the colour itself', () => {
		expect(accentRoles('#2563eb', 'dark')).toMatchObject({
			base: '#2563eb',
			wash: 'color-mix(in srgb, #2563eb 12%, transparent)',
			line: 'color-mix(in srgb, #2563eb 30%, transparent)',
		});
	});

	it('names one token per role', () => {
		expect(Object.keys(accentRoles('#2563eb', 'light')!).sort()).toEqual(Object.keys(ACCENT_ROLE_TOKENS).sort());
		expect(new Set(Object.values(ACCENT_ROLE_TOKENS)).size).toBe(Object.keys(ACCENT_ROLE_TOKENS).length);
	});

	it('keeps the default accent readable with the ink its fill takes', () => {
		// The default (no-project) accent is the corsia project's own #0083af, white ink on it: Lc 74.7 by APCA
		// against 34.6 for near-black, which is why WCAG 2's ratio (4.87 black vs 4.32 white) is not the rule here.
		const fill = parseColor('#0083af')!;
		expect(inkOn('#0083af')).toBe('#ffffff');
		expect(apcaLc(parseColor('#ffffff')!, fill)).toBeCloseTo(-74.74, 1);
		expect(apcaLc(parseColor('#0a0a0b')!, fill)).toBeCloseTo(34.64, 1);
		expect(apcaLc(parseColor('#0a0a0b')!, fill)).toBeLessThan(Math.abs(apcaLc(parseColor('#ffffff')!, fill)));
	});
});

describe('every colour form the project API accepts', () => {
	// Each form `_ACCENT_COLOR_RE` (backend/api/project_types.py) admits, with the channels and alpha it denotes.
	const FORMS: [string, string, [number, number, number], number][] = [
		['#rgb', '#ff0', [255, 255, 0], 1],
		['#rgba', '#0f08', [0, 255, 0], 0x88 / 255],
		['#rrggbb', '#2563eb', [37, 99, 235], 1],
		['#rrggbbaa', '#2563eb80', [37, 99, 235], 0x80 / 255],
		['rgb() comma', 'rgb(37, 99, 235)', [37, 99, 235], 1],
		['rgb() comma percentages', 'rgb(100%, 0%, 50%)', [255, 0, 127.5], 1],
		['rgb() space', 'rgb(37 99 235)', [37, 99, 235], 1],
		['rgb() space with / alpha', 'rgb(37 99 235 / 0.5)', [37, 99, 235], 0.5],
		['rgba() comma', 'rgba(37, 99, 235, 0.5)', [37, 99, 235], 0.5],
		['rgba() space with / alpha', 'rgba(37 99 235 / 50%)', [37, 99, 235], 0.5],
		['hsl() comma', 'hsl(120, 100%, 50%)', [0, 255, 0], 1],
		['hsl() space', 'hsl(120 100% 50%)', [0, 255, 0], 1],
		['hsl() space with / alpha', 'hsl(120 100% 50% / 0.25)', [0, 255, 0], 0.25],
		['hsla() comma', 'hsla(120, 100%, 50%, 0.4)', [0, 255, 0], 0.4],
		['hsla() space with / alpha', 'hsla(120 100% 50% / 40%)', [0, 255, 0], 0.4],
		['upper case', 'RGB(37, 99, 235)', [37, 99, 235], 1],
	];

	it.each(FORMS)('parses %s and derives readable, opaque roles from it', (_, colour, rgb, alpha) => {
		const parsed = parseCssColour(colour)!;
		expect(parsed).not.toBeNull();
		parsed.rgb.forEach((channel, i) => expect(channel).toBeCloseTo(rgb[i], 5));
		expect(parsed.alpha).toBeCloseTo(alpha, 5);
		expectReadable(colour);
		for (const theme of THEMES) {
			const { base, wash } = accentRoles(colour, theme)!;
			// An alpha is dropped: the base is then the opaque hex, and the wash mixes that.
			if (alpha < 1) {
				expect(base).toMatch(/^#[0-9a-f]{6}$/);
				expect(wash).toBe(`color-mix(in srgb, ${base} 12%, transparent)`);
			} else {
				expect(base).toBe(colour);
			}
		}
	});

	it('steps a pale #ff0 to readable accent text and keeps its own fill', () => {
		expectReadable('#ff0');
		expect(accentRoles('#ff0', 'light')!.text).not.toBe('#ffff00');
		expect(accentRoles('#ff0', 'light')!.fill).toBe('#ff0');
		expect(accentRoles('#ff0', 'light')!.onFill).toBe('#0a0a0b');
	});
});

describe('a colour that does not parse', () => {
	afterEach(() => vi.restoreAllMocks());

	// Each matches `_ACCENT_COLOR_RE` but is not a colour.
	it.each(['rgb(1, 2)', 'rgb(1, 2%, 3)', 'hsl(10%, 20%, 30%)', 'hsl(10, 20, 30)', 'rgb(1 2 3 / 4 / 5)', 'rgb(1.2.3, 4, 5)'])(
		'%s falls back to the default accent and warns once',
		(colour) => {
			const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
			expect(parseCssColour(colour)).toBeNull();
			expect(accentRoles(colour, 'light')).toBeNull();
			expect(accentRoles(colour, 'dark')).toBeNull();
			expect(warn).toHaveBeenCalledTimes(1);
			expect(warn.mock.calls[0][0]).toContain(colour);
		},
	);
});

describe('ACCENT_TEXT_SURFACES', () => {
	const css = readFileSync(resolve(__dirname, '../../app-tokens.css'), 'utf8');
	/** The theme's top-level block: the one that sets its color-scheme. */
	const block = (selector: string, scheme: string) =>
		[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].find((m) => m[1].trim() === selector && m[2].includes(`color-scheme: ${scheme};`))![2];
	const value = (text: string, token: string) => new RegExp(`(?:^|[\\s;])${token}\\s*:\\s*([^;]+);`).exec(text)![1].trim();

	it.each([
		['dark', ':root'],
		['light', '[data-theme="light"]'],
	] as const)('equals the %s theme surface tokens in app-tokens.css', (theme, selector) => {
		const text = block(selector, theme);
		expect(ACCENT_TEXT_SURFACES[theme]).toEqual(['--bg-primary', '--bg-tertiary', '--bg-hover', '--bg-active'].map((token) => value(text, token)));
	});
});
