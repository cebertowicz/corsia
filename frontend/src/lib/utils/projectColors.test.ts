import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apcaLc, contrastRatio, inkOn, over, parseColor, parseCssColour, type Rgb } from './contrast';
import {
	ACCENT_ROLE_TOKENS,
	ACCENT_SWATCHES,
	ACCENT_TEXT_SURFACES,
	accentRoles,
	FILL_INK_MIN_LC,
	SELECTION_MIN_CONTRAST,
	SELECTION_TEXT,
	SELECTION_TEXT_MIN_CONTRAST,
} from './projectColors';

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
		expect(accentRoles('#007faa', 'light')!.fill).toBe('#007faa');
		expect(accentRoles('#007faa', 'dark')!.fill).toBe('#007faa');
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
		// The default (no-project) accent is the corsia project's own #007faa, white ink on it: Lc 76.4 by APCA
		// against 32.9 for near-black. It is also the lightest step of that hue where white meets WCAG AA for body
		// text, so the default's primary buttons and bubble pass both models.
		const fill = parseColor('#007faa')!;
		expect(inkOn('#007faa')).toBe('#ffffff');
		expect(contrastRatio(parseColor('#ffffff')!, fill)).toBeGreaterThanOrEqual(4.5);
		expect(apcaLc(parseColor('#ffffff')!, fill)).toBeCloseTo(-76.45, 1);
		expect(apcaLc(parseColor('#0a0a0b')!, fill)).toBeCloseTo(32.91, 1);
		expect(apcaLc(parseColor('#0a0a0b')!, fill)).toBeLessThan(Math.abs(apcaLc(parseColor('#ffffff')!, fill)));
	});
});

/** The selection a role paints over the document page, as the browser renders it: the colour's share over the page, in 8-bit channels. */
function selectionOver(colour: string, theme: 'light' | 'dark') {
	const { selection } = accentRoles(colour, theme)!;
	const match = /^color-mix\(in srgb, (.+) (\d+)%, transparent\)$/.exec(selection);
	expect(match, `${colour} ${theme} selection ${selection}`).not.toBeNull();
	const page = parseColor(ACCENT_TEXT_SURFACES[theme][0])!;
	const mixed = over(parseColor(match![1])!, page, Number(match![2]) / 100).map(Math.round) as Rgb;
	return { share: Number(match![2]), page, mixed, selection: contrastRatio(mixed, page), text: contrastRatio(SELECTION_TEXT[theme], mixed) };
}

describe('the accent selection role', () => {
	it.each(['#007faa', ...ACCENT_SWATCHES])('reads against the document page and keeps its text readable for %s, in both themes', (colour) => {
		for (const theme of THEMES) {
			const { selection, text } = selectionOver(colour, theme);
			expect(selection, `${colour} ${theme} selection against the page`).toBeGreaterThanOrEqual(SELECTION_MIN_CONTRAST);
			expect(text, `${colour} ${theme} text over the selection`).toBeGreaterThanOrEqual(SELECTION_TEXT_MIN_CONTRAST);
		}
	});

	it('starts from 35% in light and 45% in dark and rises only for an accent that does not read there', () => {
		expect(selectionOver('#007faa', 'light').share).toBe(35);
		expect(selectionOver('#007faa', 'dark').share).toBe(45);
		// #ca8a04 composites to about 1.42:1 over white at 35%: the share rises until it reaches the floor.
		expect(selectionOver('#ca8a04', 'light').share).toBeGreaterThan(35);
		expect(selectionOver('#ca8a04', 'dark').share).toBe(45);
	});

	it('keeps the text readable across the hue spread, and where the page cannot be reached takes the strongest selection that does', () => {
		for (const colour of SPREAD) {
			for (const theme of THEMES) {
				const { share, selection, text } = selectionOver(colour, theme);
				expect(text, `${colour} ${theme} text over the selection`).toBeGreaterThanOrEqual(SELECTION_TEXT_MIN_CONTRAST);
				if (selection >= SELECTION_MIN_CONTRAST) continue;
				// The floor is out of reach: one more percent of the accent would cost the text its floor (or there is no more to add).
				if (share === 100) continue;
				const page = parseColor(ACCENT_TEXT_SURFACES[theme][0])!;
				const more = over(parseColor(colour)!, page, (share + 1) / 100).map(Math.round) as Rgb;
				expect(contrastRatio(SELECTION_TEXT[theme], more), `${colour} ${theme} at ${share + 1}%`).toBeLessThan(SELECTION_TEXT_MIN_CONTRAST);
			}
		}
	});

	it('is a share of the accent itself, so it follows the colour a project is painted', () => {
		expect(accentRoles('#2563eb', 'dark')!.selection).toMatch(/^color-mix\(in srgb, #2563eb \d+%, transparent\)$/);
		// An alpha is dropped like every role: the mix is of the opaque hex.
		expect(accentRoles('#2563eb80', 'light')!.selection).toMatch(/^color-mix\(in srgb, #2563eb \d+%, transparent\)$/);
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

	it.each([
		['dark', ':root'],
		['light', '[data-theme="light"]'],
	] as const)('puts the %s selection text on the page text token of app-tokens.css', (theme, selector) => {
		expect(SELECTION_TEXT[theme]).toEqual(parseColor(value(block(selector, theme), '--text-primary')));
	});
});
