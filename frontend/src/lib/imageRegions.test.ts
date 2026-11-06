import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { AGENT_COLORS } from '$lib/docPresence';
import { contrastRatio, over, parseHex } from '$lib/utils/contrast';
import { badgeFill, crowdedNames, describeRegion, nearestTarget, placeTag, placeTags, POINT_RADIUS, regionColor, regionFromDrag, regionRect, revealCentre, TAG_RADIUS } from './imageRegions';

const FRAME = { width: 800, height: 500 };

describe('placeTag', () => {
	// Expected values come from backend/doceditor/image_markup.py place_tag with radius 11 and gap 3,
	// so the viewer and the marked-up copy an agent reads put every tag in the same place.
	it.each([
		[{ x: 0.4, y: 0.3, w: 0.2, h: 0.2 }, { x: 304, y: 134, side: 'up-left' }],
		[{ x: 0, y: 0.3, w: 0.2, h: 0.2 }, { x: 13, y: 134, side: 'above' }],
		[{ x: 0, y: 0, w: 0.3, h: 0.2 }, { x: 256, y: 13, side: 'right' }],
		[{ x: 0, y: 0, w: 1, h: 0.5 }, { x: 14, y: 14, side: 'inside' }],
		[{ x: 0.5, y: 0.5, w: 0, h: 0 }, { x: 384.6, y: 234.6, side: 'up-left' }],
		[{ x: 0.99, y: 0.01, w: 0, h: 0 }, { x: 776.6, y: 20.4, side: 'down-left' }],
	])('matches the server renderer for %o', (region, expected) => {
		const tag = placeTag(region, FRAME);
		expect(tag.side).toBe(expected.side);
		expect(tag.x).toBeCloseTo(expected.x, 6);
		expect(tag.y).toBeCloseTo(expected.y, 6);
	});

	it('puts a box tag outside its top-left corner and a point tag up-left of the point', () => {
		const box = placeTag({ x: 0.4, y: 0.3, w: 0.2, h: 0.2 }, FRAME);
		expect(box.x).toBeLessThan(0.4 * 800);
		expect(box.y).toBeLessThan(0.3 * 500);
		const point = placeTag({ x: 0.5, y: 0.5, w: 0, h: 0 }, FRAME);
		expect(point.x).toBeLessThan(0.5 * 800);
		expect(point.y).toBeLessThan(0.5 * 500);
	});

	it('keeps the badge on the image at its edges by taking the next spot', () => {
		// Box on the top edge: no room above-left, so left of the corner, still clear of the box.
		expect(placeTag({ x: 0.4, y: 0, w: 0.2, h: 0.2 }, FRAME)).toMatchObject({ side: 'left', x: 304, y: 13 });
		// Box on the left edge: no room left, so above.
		expect(placeTag({ x: 0, y: 0.3, w: 0.2, h: 0.2 }, FRAME)).toMatchObject({ side: 'above', x: 13, y: 134 });
		// Point on the left edge: up-right; on the top edge: down-right.
		expect(placeTag({ x: 0, y: 0.5, w: 0, h: 0 }, FRAME)).toMatchObject({ side: 'up-right' });
		expect(placeTag({ x: 0.5, y: 0, w: 0, h: 0 }, FRAME)).toMatchObject({ side: 'down-right' });
		for (const region of [{ x: 0.4, y: 0, w: 0.2, h: 0.2 }, { x: 0, y: 0.5, w: 0, h: 0 }, { x: 0.5, y: 0, w: 0, h: 0 }, { x: 1, y: 1, w: 0, h: 0 }]) {
			const tag = placeTag(region, FRAME);
			expect(tag.x - (TAG_RADIUS + 2)).toBeGreaterThanOrEqual(-1e-9);
			expect(tag.y - (TAG_RADIUS + 2)).toBeGreaterThanOrEqual(-1e-9);
			expect(tag.x + (TAG_RADIUS + 2)).toBeLessThanOrEqual(800 + 1e-9);
			expect(tag.y + (TAG_RADIUS + 2)).toBeLessThanOrEqual(500 + 1e-9);
		}
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

	it("keeps each tag off every other comment's dot and box while a clear spot fits", () => {
		const covers = (tag: { x: number; y: number }, region: { x: number; y: number; w: number; h: number }) => {
			const r = TAG_RADIUS + 2;
			if (region.w <= 0 && region.h <= 0) return Math.hypot(tag.x - region.x * 800, tag.y - region.y * 500) < r + POINT_RADIUS + 2;
			const nx = Math.min(Math.max(tag.x, region.x * 800), (region.x + region.w) * 800);
			const ny = Math.min(Math.max(tag.y, region.y * 500), (region.y + region.h) * 500);
			return Math.hypot(tag.x - nx, tag.y - ny) < r;
		};
		// A point whose first spot lands on a later point's dot, and a box whose left spot sits on a neighbour's box edge.
		const cases = [
			[{ x: 0.5, y: 0.5, w: 0, h: 0 }, { x: 0.5 + 15.4 / 800, y: 0.5 - 15.4 / 500, w: 0, h: 0 }],
			[{ x: 0.4, y: 0.4, w: 0.1, h: 0.1 }, { x: 0.3, y: 0.35, w: 0.08, h: 0.1 }],
		];
		for (const regions of cases) {
			const tags = placeTags(regions, FRAME);
			tags.forEach((tag, i) => regions.forEach((region, j) => { if (i !== j) expect(covers(tag, region), `tag ${i + 1} on mark ${j + 1}`).toBe(false); }));
		}
	});

	it('lets a crowded tag overlap rather than leave the image', () => {
		const tags = placeTags([{ x: 0, y: 0, w: 1, h: 0.5 }, { x: 0, y: 0, w: 1, h: 0.5 }], FRAME);
		expect(tags.map((tag) => tag.side)).toEqual(['inside', 'inside']);
	});
});

// Placement stays within the image; the viewer's gutter uses imageGutter.ts.
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

describe('revealCentre', () => {
	const frame = new DOMRect(100, 50, 800, 500);
	it('centres a box together with its tag, so the tag lands clear of the viewport edge', () => {
		// On screen the box runs 420 to 580 across and 200 to 300 down; its tag sits left of the top-left corner.
		const centre = revealCentre({ x: 0.4, y: 0.3, w: 0.2, h: 0.2 }, new DOMRect(395, 189, 22, 22), frame);
		expect(centre.x).toBeCloseTo(((395 + 580) / 2 - 100) / 800);
		expect(centre.y).toBeCloseTo(((189 + 300) / 2 - 50) / 500);
	});

	it('centres the box alone when its tag is not on screen', () => {
		expect(revealCentre({ x: 0.4, y: 0.3, w: 0.2, h: 0.2 }, null, frame)).toEqual({ x: 0.5, y: 0.4 });
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

describe('crowdedNames', () => {
	function element(html: string, rect: [number, number, number, number]) {
		const holder = document.createElement('div');
		holder.innerHTML = html;
		const node = holder.firstElementChild as HTMLElement;
		node.getBoundingClientRect = () => new DOMRect(...rect);
		return node;
	}
	function tagWithName(id: string, name: [number, number, number, number]) {
		const tag = element(`<button data-region-tag="${id}" data-comment-id="${id}"><span class="name"></span></button>`, [0, 0, 0, 0]);
		tag.querySelector<HTMLElement>('.name')!.getBoundingClientRect = () => new DOMRect(...name);
		return tag;
	}
	it("hides a name over another tag or another comment's point, but not over its own point or a leaving tag", () => {
		const layer = document.createElement('div');
		const leavingTag = element('<button data-region-tag="e" data-comment-id="e"></button>', [410, 8, 22, 22]);
		leavingTag.inert = true;
		layer.append(
			tagWithName('a', [10, 10, 40, 12]), element('<button data-region-tag="b" data-comment-id="b"></button>', [30, 8, 22, 22]),
			tagWithName('c', [200, 10, 40, 12]), element('<div data-region-dot="c"></div>', [210, 8, 14, 14]),
			tagWithName('d', [400, 10, 40, 12]), leavingTag,
			tagWithName('f', [600, 10, 40, 12]), element('<div data-region-dot="g"></div>', [610, 8, 14, 14]),
		);
		expect([...crowdedNames(layer)].sort()).toEqual(['a', 'f']);
	});
});

describe('badgeFill', () => {
	const INK = parseHex('#111111')!;
	const digitOver = (fill: string, backdrop: number) => contrastRatio(INK, over(parseHex(fill)!, [backdrop, backdrop, backdrop], 0.8));

	it('steps a colour whose digit fails on black lighter, until the digit reads on black and on white', () => {
		expect(digitOver('#8b5cf6', 0)).toBeLessThan(4.5);
		const fill = badgeFill('#8b5cf6');
		expect(fill).not.toBe('#8b5cf6');
		expect(digitOver(fill, 0)).toBeGreaterThanOrEqual(4.5);
		expect(digitOver(fill, 255)).toBeGreaterThanOrEqual(4.5);
	});

	it('leaves a colour whose digit already reads as it came', () => {
		expect(badgeFill('#22c55e')).toBe('#22c55e');
		expect(badgeFill('#2DD4BF')).toBe('#2DD4BF');
	});

	it('reads the digit on every agent colour, over black and over white', () => {
		for (const colour of AGENT_COLORS) {
			for (const backdrop of [0, 255]) expect(digitOver(badgeFill(colour), backdrop)).toBeGreaterThanOrEqual(4.5);
		}
	});

	it('reads the digit in the ink the token gives it', () => {
		const tokens = readFileSync(resolve(__dirname, '../app-tokens.css'), 'utf8');
		expect(/--image-region-ink:\s*#111;/.test(tokens)).toBe(true);
	});

	it('returns what is not a colour as it came', () => {
		expect(badgeFill('var(--accent)')).toBe('var(--accent)');
	});
});
