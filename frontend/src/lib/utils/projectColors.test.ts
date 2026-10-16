import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { contrastRatio, inkOn, parseColor, parseCssColour } from './contrast';
import { ACCENT_ROLE_TOKENS, ACCENT_SWATCHES, ACCENT_TEXT_SURFACES, accentRoles } from './projectColors';

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
		expect(contrastRatio(parseColor(fill)!, parseColor(onFill)!), `${colour} ${theme} ink on fill`).toBeGreaterThanOrEqual(4.5);
	}
}

describe('accentRoles', () => {
	it('inks the fill it paints at 4.5:1 or above in both themes across the hue spread', () => {
		for (const colour of SPREAD) {
			for (const theme of THEMES) {
				const { fill, onFill } = accentRoles(colour, theme)!;
				expect(contrastRatio(parseColor(fill)!, parseColor(onFill)!), `${colour} ${theme}`).toBeGreaterThanOrEqual(4.5);
			}
		}
	});

	it('fills with the colour itself unless no ink reaches 4.5:1 on it', () => {
		expect(accentRoles('#2563eb', 'light')!.fill).toBe('#2563eb');
		expect(accentRoles('#16a34a', 'dark')!.fill).toBe('#16a34a');
		// Near the crossover neither ink reaches 4.5:1 on the raw colour, so the fill steps away from its ink.
		const crossover = 'hsl(220, 50%, 54%)';
		const raw = parseColor(crossover)!;
		expect(Math.max(contrastRatio(raw, [255, 255, 255]), contrastRatio(raw, [10, 10, 11]))).toBeLessThan(4.5);
		const { fill, onFill } = accentRoles(crossover, 'light')!;
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
		expect(Object.keys(accentRoles('#2563eb', 'light')!).sort()).toEqual(Object.keys(ACCENT_ROLE_TOKENS).sort());
		expect(new Set(Object.values(ACCENT_ROLE_TOKENS)).size).toBe(Object.keys(ACCENT_ROLE_TOKENS).length);
	});

	it('keeps CORSIA blue readable with each default theme ink', () => {
		expect(contrastRatio(parseColor('#2563eb')!, parseColor('#ffffff')!)).toBeCloseTo(5.17, 2);
		expect(contrastRatio(parseColor('#60a5fa')!, parseColor('#0a0a0b')!)).toBeCloseTo(7.78, 2);
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

	it('steps a pale #ff0 to readable accent text and inks its fill at 4.5:1 in both themes', () => {
		expectReadable('#ff0');
		expect(accentRoles('#ff0', 'light')!.text).not.toBe('#ffff00');
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
		expect(ACCENT_TEXT_SURFACES[theme]).toEqual(['--bg-primary', '--bg-tertiary', '--bg-hover'].map((token) => value(text, token)));
	});
});
