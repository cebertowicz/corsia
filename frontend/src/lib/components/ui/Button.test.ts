import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// jsdom does not apply a component's scoped CSS, so the tap area is read from the rules the primitive ships.
const css = readFileSync(resolve(process.cwd(), 'src/lib/components/ui/Button.svelte'), 'utf8');
const BORDER = 1;
const MIN_TAP = 44;

const heightOf = (size: string) => Number(css.match(new RegExp(`\\.btn\\.${size} \\{ height: (\\d+)px`))?.[1]);
const insetOf = (size: string) => Number(css.match(new RegExp(`\\.btn\\.${size}::after \\{ inset: -(\\d+)px`))?.[1]);

describe('Button tap area on touch screens', () => {
	// The ::after box is positioned from the padding box, so it starts inside the border: area = height - 2 * border + 2 * inset.
	for (const size of ['sm', 'md']) {
		it(`${size} reaches ${MIN_TAP}px tall, counting from inside the border`, () => {
			expect(heightOf(size)).toBeGreaterThan(0);
			expect(insetOf(size)).toBeGreaterThan(0);
			expect(heightOf(size) - 2 * BORDER + 2 * insetOf(size)).toBeGreaterThanOrEqual(MIN_TAP);
		});
	}

	it('uses the measured insets: sm -9px, md -7px', () => {
		expect(insetOf('sm')).toBe(9);
		expect(insetOf('md')).toBe(7);
	});
});
