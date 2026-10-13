import { describe, expect, it } from 'vitest';

/**
 * A `:hover` rule outside `@media (hover: hover)` sticks after a tap on a touch screen, so a
 * tapped row or toggle keeps its hover chip (corsia#429). The Activity surfaces already guard
 * theirs, e.g. `activity/ActivityPlanRow.svelte`'s `.row-summary:hover` (line 238).
 */
const sources = import.meta.glob('../components/plans/*.svelte', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;

/** Every `:hover` selector that is not inside an `@media (hover: hover)` block, as `file: selector`. */
function unguarded(file: string, source: string): string[] {
	const css = source.slice(source.indexOf('<style'));
	const stack: boolean[] = [];
	const found: string[] = [];
	let header = '';
	for (let at = 0; at < css.length; at += 1) {
		const char = css[at];
		if (char === '{') {
			stack.push(/@media[^{]*\(hover:\s*hover\)/.test(header));
			header = '';
		} else if (char === '}') {
			stack.pop();
			header = '';
		} else if (char === ';') {
			header = '';
		} else {
			header += char;
		}
		if (char === '{' && /:hover/.test(css.slice(Math.max(0, css.lastIndexOf('}', at - 1) + 1), at)) && !stack.some(Boolean)) {
			found.push(`${file}: ${css.slice(Math.max(0, css.lastIndexOf('}', at - 1) + 1), at).trim()}`);
		}
	}
	return found;
}

describe('plans: hover rules are for a pointer', () => {
	const files = Object.entries(sources);

	it('finds the plans components', () => {
		expect(files.length).toBeGreaterThan(10);
	});

	it('has no :hover rule outside @media (hover: hover)', () => {
		const loose = files.flatMap(([file, source]) => unguarded(file.split('/').pop()!, source));
		expect(loose).toEqual([]);
	});

	it('keeps the hover rules it guards', () => {
		const guarded = files.filter(([, source]) => /@media \(hover: hover\)\s*{[^}]*:hover/.test(source));
		expect(guarded.length).toBeGreaterThanOrEqual(7);
	});
});
