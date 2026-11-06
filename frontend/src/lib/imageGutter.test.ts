import { describe, expect, it } from 'vitest';
import cases from './__tests__/fixtures/image-gutter-parity.json';
import { gutterTags } from './imageGutter';

describe('gutter tags', () => {
	it.each(cases)('$name matches the server contract', ({ regions, width, height, radius, gap, expected }) => {
		const actual = gutterTags(regions, { width, height }, radius, gap);
		actual.forEach((tag, index) => {
			expect(tag.side).toBe(expected[index].side);
			expect(tag.x).toBeCloseTo(expected[index].x);
			expect(tag.y).toBeCloseTo(expected[index].y);
			expect(tag.anchor.x).toBeCloseTo(expected[index].anchor.x);
			expect(tag.anchor.y).toBeCloseTo(expected[index].anchor.y);
		});
	});
});
