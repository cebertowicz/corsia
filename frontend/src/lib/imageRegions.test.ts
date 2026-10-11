import { describe, expect, it } from 'vitest';
import { AGENT_COLORS } from '$lib/docPresence';
import { describeRegion, nearestTarget, placeTag, placeTags, regionColor, regionFromDrag, regionRect, TAG_RADIUS } from './imageRegions';

const FRAME = { width: 800, height: 500 };

describe('placeTag', () => {
	// Expected values come from backend/doceditor/image_markup.py place_tag with radius 11 and gap 3,
	// so the viewer and the marked-up copy an agent reads put every tag in the same place.
	it.each([
		[{ x: 0.4, y: 0.3, w: 0.2, h: 0.2 }, { x: 304, y: 163, side: 'left' }],
		[{ x: 0, y: 0.3, w: 0.2, h: 0.2 }, { x: 13, y: 134, side: 'above' }],
		[{ x: 0, y: 0, w: 0.3, h: 0.2 }, { x: 256, y: 13, side: 'right' }],
		[{ x: 0, y: 0, w: 1, h: 0.5 }, { x: 14, y: 14, side: 'inside' }],
		[{ x: 0.5, y: 0.5, w: 0, h: 0 }, { x: 415.4, y: 234.6, side: 'up-right' }],
		[{ x: 0.99, y: 0.01, w: 0, h: 0 }, { x: 776.6, y: 20.4, side: 'down-left' }],
	])('matches the server renderer for %o', (region, expected) => {
		const tag = placeTag(region, FRAME);
		expect(tag.side).toBe(expected.side);
		expect(tag.x).toBeCloseTo(expected.x, 6);
		expect(tag.y).toBeCloseTo(expected.y, 6);
	});

	it('never covers the box it points at unless every outside spot is clipped', () => {
		for (const region of [{ x: 0.2, y: 0.2, w: 0.3, h: 0.3 }, { x: 0, y: 0.5, w: 0.2, h: 0.2 }, { x: 0, y: 0, w: 0.5, h: 0.4 }]) {
			const tag = placeTag(region, FRAME);
			const inside = tag.x > region.x * 800 && tag.x < (region.x + region.w) * 800 && tag.y > region.y * 500 && tag.y < (region.y + region.h) * 500;
			expect(inside).toBe(false);
		}
	});

	it('keeps the whole tag on an image smaller than the tag', () => {
		const tag = placeTag({ x: 0, y: 0, w: 1, h: 1 }, { width: 10, height: 10 });
		expect(tag).toMatchObject({ x: 5, y: 5, side: 'inside' });
	});
});

describe('placeTags', () => {
	it('keeps each tag clear of the ones before it on close neighbours', () => {
		// Points on adjacent buttons of a screenshot, a few pixels apart at the default canvas.
		const regions = [{ x: 0.5, y: 0.5, w: 0, h: 0 }, { x: 0.51, y: 0.5, w: 0, h: 0 }, { x: 0.5, y: 0.52, w: 0, h: 0 }];
		const tags = placeTags(regions, FRAME);
		for (let i = 0; i < tags.length; i++) {
			for (let j = i + 1; j < tags.length; j++) {
				expect(Math.hypot(tags[i].x - tags[j].x, tags[i].y - tags[j].y)).toBeGreaterThanOrEqual(2 * (TAG_RADIUS + 2));
			}
		}
	});

	it('lets a crowded tag overlap rather than leave the image', () => {
		const tags = placeTags([{ x: 0, y: 0, w: 1, h: 0.5 }, { x: 0, y: 0, w: 1, h: 0.5 }], FRAME);
		expect(tags.map((tag) => tag.side)).toEqual(['inside', 'inside']);
	});
});

describe('regionFromDrag', () => {
	it('normalises a drag in any direction to a box in fractions', () => {
		expect(regionFromDrag({ x: 400, y: 300 }, { x: 80, y: 100 }, FRAME)).toEqual({ x: 0.1, y: 0.2, w: 0.4, h: 0.4 });
	});

	it('treats a short drag as a point where it started', () => {
		expect(regionFromDrag({ x: 400, y: 250 }, { x: 403, y: 252 }, FRAME)).toEqual({ x: 0.5, y: 0.5, w: 0, h: 0 });
	});

	it('clamps a drag that leaves the image to its edges', () => {
		expect(regionFromDrag({ x: 600, y: 400 }, { x: 900, y: 700 }, FRAME)).toEqual({ x: 0.75, y: 0.8, w: 0.25, h: 0.2 });
	});
});

describe('regionRect', () => {
	it('places a box and a point dot on screen for the frame as drawn', () => {
		const frame = new DOMRect(100, 50, 400, 250);
		expect(regionRect({ x: 0.5, y: 0.2, w: 0.25, h: 0.4 }, frame)).toMatchObject({ left: 300, top: 100, width: 100, height: 100 });
		expect(regionRect({ x: 0.5, y: 0.2, w: 0, h: 0 }, frame)).toMatchObject({ left: 293, top: 93, width: 14, height: 14 });
	});
});

describe('nearestTarget', () => {
	it('gives overlapping 44 px targets to the nearer centre, and nothing outside them', () => {
		const targets = [{ id: 'a', x: 100, y: 100 }, { id: 'b', x: 120, y: 100 }];
		expect(nearestTarget(targets, { x: 108, y: 100 })).toBe('a');
		expect(nearestTarget(targets, { x: 112, y: 104 })).toBe('b');
		expect(nearestTarget(targets, { x: 200, y: 200 })).toBeNull();
	});
});

describe('regionColor and describeRegion', () => {
	it("uses an agent's own colour and the project accent for a person", () => {
		expect(AGENT_COLORS).toContain(regionColor({ authorKind: 'agent', authorThreadId: 'thread-1' }, 'p1', '#123456'));
		expect(regionColor({ authorKind: 'user' }, 'p1', '#123456')).toBe('#123456');
	});

	it('describes boxes and points in words', () => {
		expect(describeRegion({ x: 0.1, y: 0.2, w: 0.3, h: 0.1 })).toBe('box 30% wide, 10% tall at 10% across, 20% down');
		expect(describeRegion({ x: 0.7, y: 0.6, w: 0, h: 0 })).toBe('point at 70% across, 60% down');
	});
});
