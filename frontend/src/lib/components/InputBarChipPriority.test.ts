import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// jsdom has no layout, so this pins the structural contract, not the width. The row's real width is
// proven by a render at phone widths and by a layout probe measuring fractional widths.
const css = readFileSync(resolve(__dirname, 'InputBar.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

/** Every declaration of every rule whose selector is exactly `selector`, later ones winning. */
function declarations(selector: string): Record<string, string> {
	const out: Record<string, string> = {};
	for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
		if (rule[1].trim() !== selector) continue;
		for (const part of rule[2].split(';')) {
			const [name, ...value] = part.split(':');
			if (name.trim() && value.length) out[name.trim()] = value.join(':').trim();
		}
	}
	return out;
}

/** The flex-shrink a rule sets, from `flex-shrink` or the `flex` shorthand; 1 when it sets none. */
function shrink(rule: Record<string, string>): number {
	if (rule['flex-shrink'] !== undefined) return Number(rule['flex-shrink']);
	const shorthand = rule.flex?.split(/\s+/);
	return shorthand && shorthand.length >= 2 && !Number.isNaN(Number(shorthand[1])) ? Number(shorthand[1]) : 1;
}

describe('composer action row: who gives way first', () => {
	const model = declarations('.model-chip');
	const context = declarations('.context-chip');

	it('the context chip shrinks far faster than the model chip, down to its ring', () => {
		expect(shrink(context)).toBeGreaterThan(shrink(model) * 100);
		expect(parseInt(context['min-width'], 10)).toBeGreaterThan(0);
	});

	// Flex shares a deficit in proportion to flex-shrink times width, so a name chip left at 1 takes a
	// fractional-pixel sliver (cut to "Sonnet 5\u2026" at 375px) while the context chip still has room.
	// Its share must be effectively zero, which only a render at fractional widths shows; this pins it.
	it('the model chip takes effectively none of the deficit while the context chip has room', () => {
		expect(shrink(model)).toBeLessThanOrEqual(0.01);
		expect(shrink(context) / shrink(model)).toBeGreaterThanOrEqual(100_000);
	});

	it('the context label leaves whole: it wraps to a line the chip clips, never half-cut', () => {
		expect(context['flex-wrap']).toBe('wrap');
		expect(context.overflow).toBe('hidden');
		expect(context.height).toMatch(/^\d+px$/);
	});

	it('the model name keeps its own ellipsis without an artificial desktop cap, and never refuses to shrink', () => {
		expect(model['max-width']).toBeUndefined();
		expect(model['min-width']).toBe('0');
		expect(shrink(model)).toBeGreaterThan(0);
		expect(declarations('.model-chip-name')['text-overflow']).toBe('ellipsis');
	});

	it('no width query hides the context label ahead of the shrink order', () => {
		expect(css).not.toMatch(/@container[^{]*\{\s*\.context-chip span/);
	});
});
