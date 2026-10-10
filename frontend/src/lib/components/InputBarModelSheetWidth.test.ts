import { describe, expect, it } from 'vitest';
import sheetSource from './InputBarModelSheet.svelte?raw';

/** Each `selector { body }` rule in a stylesheet, flattened past at-rule wrappers. */
function rules(css: string): { selector: string; body: string }[] {
	const out: { selector: string; body: string }[] = [];
	const pattern = /([^{}]+)\{([^{}]*)\}/g;
	for (const match of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(pattern)) {
		out.push({ selector: match[1].trim(), body: match[2] });
	}
	return out;
}

const sheetStyle = sheetSource.match(/<style[^>]*>([\s\S]*?)<\/style>/)?.[1] ?? '';

describe('model picker sheet width', () => {
	it('leaves the sheet width to the shared action sheet', () => {
		// A scoped rule outranks the global .action-sheet cap and lets the picker span the column.
		const widthRules = rules(sheetStyle).filter(
			({ selector, body }) =>
				selector.split(',').some((part) => /\.model-sheet\b/.test(part)) && /(^|[\s;])(max-)?width\s*:/.test(body)
		);
		expect(widthRules).toEqual([]);
	});
});
