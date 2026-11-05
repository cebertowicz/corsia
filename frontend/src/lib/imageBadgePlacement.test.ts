import { describe, expect, it } from 'vitest';
import { badgeTags } from './imageGutter';
import parity from './__tests__/fixtures/image-badge-parity.json';

const size = { width: 800, height: 500 };
const region = { x: 0, y: 0, w: 1, h: 0.5 };

describe('badge placement', () => {
	it('separates nearby badges at phone Fit and connects any moved outside', () => {
		const shown = { width: 271, height: 169.375 };
		const tags = badgeTags(parity[1].regions, shown, 11, 3);
		for (let i = 0; i < tags.length; i++) for (let j = i + 1; j < tags.length; j++) {
			expect(Math.hypot(tags[i].x - tags[j].x, tags[i].y - tags[j].y)).toBeGreaterThanOrEqual(22);
		}
		const outside = tags.filter((tag) => tag.x < 0 || tag.x > shown.width || tag.y < 0 || tag.y > shown.height);
		expect(outside.length).toBeGreaterThan(0);
		for (const tag of outside) expect(tag.anchor).toBeDefined();
	});
	it.each(parity)('matches the server placement contract', ({ regions, width, height, radius, gap, expected }) => {
		expect(badgeTags(regions, { width, height }, radius, gap)).toEqual(expected);
	});
	it('keeps a lone badge on the picture', () => {
		const [badge] = badgeTags([region], size, 11, 3);
		expect(badge.x).toBeGreaterThan(0);
		expect(badge.x).toBeLessThan(size.width);
		expect(badge.y).toBeGreaterThan(0);
		expect(badge.y).toBeLessThan(size.height);
	});

	it('moves a badge that would overlap off the picture', () => {
		const [first, second] = badgeTags([region, region], size, 11, 3);
		expect(first.x).toBeGreaterThan(0);
		expect(first.y).toBeGreaterThan(0);
		expect(second.x < 0 || second.x > size.width || second.y < 0 || second.y > size.height).toBe(true);
	});
});
