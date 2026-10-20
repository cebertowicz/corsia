import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// WCAG AA guard for the shared text tokens: every text token must reach 4.5:1
// on every background it is rendered on, in both themes, computed from the
// literal values in app-tokens.css.

type RGB = [number, number, number];
const MIN = 4.5;

const css = readFileSync(resolve(__dirname, 'app-tokens.css'), 'utf8');

function stripAtRules(src: string): string {
	let out = '';
	let i = 0;
	while (i < src.length) {
		if (src[i] === '@') {
			const open = src.indexOf('{', i);
			let depth = 1;
			let j = open + 1;
			while (depth > 0 && j < src.length) {
				if (src[j] === '{') depth++;
				else if (src[j] === '}') depth--;
				j++;
			}
			i = j;
		} else {
			out += src[i++];
		}
	}
	return out;
}

function varsFor(selector: string): Record<string, string> {
	const vars: Record<string, string> = {};
	const re = /([^{}]+)\{([^{}]*)\}/g;
	for (const m of stripAtRules(css).matchAll(re)) {
		if (m[1].trim() !== selector) continue;
		for (const d of m[2].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) vars[d[1]] = d[2].trim();
	}
	return vars;
}

const dark = varsFor(':root');
const light = { ...dark, ...varsFor('[data-theme="light"]') };

function resolveVar(vars: Record<string, string>, name: string): string {
	let v = vars[name];
	for (let n = 0; v?.startsWith('var(') && n < 5; n++) {
		v = vars[v.slice(4, -1).split(',')[0].trim()];
	}
	return v;
}

function parseColor(v: string): { rgb: RGB; a: number } {
	const hex = v.match(/^#([0-9a-f]{6})$/i);
	if (hex) {
		const n = parseInt(hex[1], 16);
		return { rgb: [(n >> 16) & 255, (n >> 8) & 255, n & 255], a: 1 };
	}
	const rgba = v.match(/^rgba?\(([^)]+)\)$/);
	if (rgba) {
		const p = rgba[1].split(',').map((x) => parseFloat(x));
		return { rgb: [p[0], p[1], p[2]], a: p[3] ?? 1 };
	}
	// The accent roles are authored the way `accentRoles()` writes them: a colour mixed with transparent, which
	// composites over its surface exactly as `over()` below does.
	const mix = /^color-mix\(in srgb,\s*#([0-9a-f]{6})\s+(\d+(?:\.\d+)?)%,\s*transparent\)$/i.exec(v.trim());
	if (mix) {
		const n = parseInt(mix[1], 16);
		return { rgb: [(n >> 16) & 255, (n >> 8) & 255, n & 255], a: parseFloat(mix[2]) / 100 };
	}
	throw new Error(`unparseable colour: ${v}`);
}

function over(fg: { rgb: RGB; a: number }, base: RGB): RGB {
	return fg.rgb.map((c, i) => Math.round(fg.a * c + (1 - fg.a) * base[i])) as RGB;
}

function luminance([r, g, b]: RGB): number {
	const f = (v: number) => {
		const s = v / 255;
		return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
	};
	return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function ratio(a: RGB, b: RGB): number {
	const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
	return (hi + 0.05) / (lo + 0.05);
}

const NEUTRAL = ['--bg-primary', '--bg-secondary', '--bg-tertiary', '--bg-hover', '--bg-active'];
const BASES = ['--bg-primary', '--bg-secondary'];

// Neutral surfaces plus translucent tinted rows composited over the two base
// surfaces. `row-hover` is the text-primary 6% hover wash.
function backgrounds(vars: Record<string, string>, tints: string[]): Record<string, RGB> {
	const out: Record<string, RGB> = {};
	for (const n of NEUTRAL) out[n] = parseColor(resolveVar(vars, n)).rgb;
	for (const t of tints) {
		const fill =
			t === 'row-hover'
				? { rgb: parseColor(resolveVar(vars, '--text-primary')).rgb, a: 0.06 }
				: parseColor(resolveVar(vars, t));
		for (const b of BASES) out[`${t} on ${b}`] = over(fill, out[b]);
	}
	return out;
}

const ALL_TINTS = ['--accent-wash', '--accent-bg-hover', '--green-bg', '--red-bg', '--amber-bg', 'row-hover'];
// text token -> tinted rows it is rendered on, beyond the neutral surfaces
const TEXT: Record<string, string[]> = {
	'--text-primary': ALL_TINTS,
	'--text-secondary': ALL_TINTS,
	'--text-muted': ALL_TINTS,
	'--text-tertiary': ALL_TINTS,
	'--accent-text': ['--accent-wash', '--accent-bg-hover'],
	'--purple-text': ['--purple-bg'],
	'--green-text': ['--green-bg'],
	'--red-text': ['--red-bg'],
	'--amber-text': ['--amber-bg'],
};

for (const [theme, vars] of [['dark', dark], ['light', light]] as const) {
	describe(`${theme} theme text contrast`, () => {
		for (const [token, tints] of Object.entries(TEXT)) {
			it(`${token} reaches ${MIN}:1 on every background`, () => {
				const fg = parseColor(resolveVar(vars, token)).rgb;
				const failures = Object.entries(backgrounds(vars, tints))
					.map(([name, bg]) => ({ name, r: ratio(fg, bg) }))
					.filter((x) => x.r < MIN)
					.map((x) => `${x.name}: ${x.r.toFixed(2)}`);
				expect(failures).toEqual([]);
			});
		}

		it('keeps three distinct levels: secondary at least 1.3x tertiary on bg-primary', () => {
			const bg = parseColor(resolveVar(vars, '--bg-primary')).rgb;
			const r = (t: string) => ratio(parseColor(resolveVar(vars, t)).rgb, bg);
			expect(r('--text-primary')).toBeGreaterThan(r('--text-secondary'));
			expect(r('--text-secondary') / r('--text-tertiary')).toBeGreaterThanOrEqual(1.3);
		});
	});
}

describe('color-scheme', () => {
	it('is set on the root per theme', () => {
		expect(css).toMatch(/:root\s*\{\s*color-scheme:\s*dark;/);
		expect(css).toMatch(/\[data-theme="light"\]\s*\{\s*color-scheme:\s*light;/);
	});
});
