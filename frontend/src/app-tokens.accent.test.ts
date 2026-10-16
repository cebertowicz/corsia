import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { ACCENT_ROLE_TOKENS, type AccentRoles, accentRoles } from './lib/utils/projectColors';

/**
 * The default (no-project) accent IS a project colour: the corsia project's own `#007faa`. So the defaults in
 * `app-tokens.css` must be exactly what `accentRoles(ACCENT_DEFAULT, theme)` returns — anything else and the
 * workspace scope and the corsia project render two different blues from one brand. The literals drift, so they
 * are pinned to the function rather than to each other.
 */

const ACCENT_DEFAULT = '#007faa';
const css = readFileSync(resolve(__dirname, 'app-tokens.css'), 'utf8');

/** Drop at-rules (`@supports`), keeping only the top-level rule bodies that set the defaults. */
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

/** Every declaration of `selector`, merging the blocks that repeat it. */
function varsFor(selector: string): Record<string, string> {
	const vars: Record<string, string> = {};
	for (const m of stripAtRules(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
		if (m[1].trim() !== selector) continue;
		for (const d of m[2].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) vars[d[1]] = d[2].trim();
	}
	return vars;
}

const dark = varsFor(':root');
const light = { ...dark, ...varsFor('[data-theme="light"]') };

function resolveVar(vars: Record<string, string>, name: string): string {
	let value = vars[name];
	for (let n = 0; value?.startsWith('var(') && n < 5; n++) {
		value = vars[value.slice(4, -1).split(',')[0].trim()];
	}
	return value;
}

const THEMES: ['light' | 'dark', Record<string, string>][] = [
	['dark', dark],
	['light', light],
];

describe('the default accent is the project blue', () => {
	it.each(THEMES)('sets every %s accent role to what accentRoles() returns for it', (theme, vars) => {
		const roles = accentRoles(ACCENT_DEFAULT, theme)!;
		expect(roles, `${ACCENT_DEFAULT} must parse`).not.toBeNull();
		for (const [role, token] of Object.entries(ACCENT_ROLE_TOKENS) as [keyof AccentRoles, string][]) {
			expect(resolveVar(vars, token), `${token} (${theme})`).toBe(roles[role]);
		}
	});

	it.each(THEMES)('derives hover and subtle from the %s accent, the way a project scope does', (theme, vars) => {
		const roles = accentRoles(ACCENT_DEFAULT, theme)!;
		expect(resolveVar(vars, '--accent-hover'), `--accent-hover (${theme})`).toBe(roles.base);
		expect(resolveVar(vars, '--accent-subtle'), `--accent-subtle (${theme})`).toBe(roles.wash);
		// Default-only: no project scope paints it, so it aliases the accent's own tint rather than keeping a
		// hand-authored shade that would not follow a project's colour.
		expect(resolveVar(vars, '--accent-bg-hover'), `--accent-bg-hover (${theme})`).toBe(roles.wash);
	});

	it.each(THEMES)('paints the %s accent fill with white ink', (theme, vars) => {
		expect(resolveVar(vars, '--accent-on-fill')).toBe('#ffffff');
		expect(resolveVar(vars, '--accent-fill')).toBe(ACCENT_DEFAULT);
	});

	it('keeps the wider-gamut block off the accent', () => {
		// #007faa is authored as sRGB; a display-p3 variant would be an invented colour accentRoles() cannot
		// derive, and it would make the default disagree with a project painted the same hex.
		const at = css.indexOf('@supports (color: color(display-p3');
		expect(at).toBeGreaterThan(-1);
		const open = css.indexOf('{', at);
		let depth = 0;
		let end = open;
		for (let i = open; i < css.length; i++) {
			if (css[i] === '{') depth++;
			else if (css[i] === '}' && --depth === 0) {
				end = i;
				break;
			}
		}
		const block = css.slice(open, end + 1);
		expect(block.match(/--accent[\w-]*\s*:/g) ?? []).toEqual([]);
		expect(block).toContain('--sky');
	});
});
