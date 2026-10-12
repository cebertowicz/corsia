/**
 * Geometry for comments on part of an image (DCOL-FR13.3, FR13.4).
 *
 * A region is a box or a point in fractions of the image, origin top-left, so it
 * holds at any zoom. Tags follow the same rule as the marked-up copy an agent
 * gets back (`backend/doceditor/image_markup.py` `place_tag`): a tag never covers
 * what its box points at. A box's tag tries left of its top-left corner, above
 * it, then right of the box, and only when the image edge clips all three sits
 * inside the box's corner. A point's tag tries up-right, then the other diagonals.
 * A spot over another comment's dot or box, or over a tag placed before it, is
 * passed over while a clear one fits.
 */
import type { ImageRegion } from '$lib/api/docs';
import { agentColor } from '$lib/docPresence';
import { projectAccent } from '$lib/utils/projectColors';

export type { ImageRegion };
export type Point = { x: number; y: number };
export type Size = { width: number; height: number };
export type TagSide = 'left' | 'above' | 'right' | 'inside' | 'up-right' | 'up-left' | 'down-right' | 'down-left';
/** Another comment's mark in pixels of the frame: a point's dot, ring included, or a box. */
export type Mark = { x: number; y: number; r: number } | { x0: number; y0: number; x1: number; y1: number };

/** A tag is a 22 px disc; its white ring adds 2 px. */
export const TAG_RADIUS = 11;
export const TAG_GAP = 3;
/** A point pin is a 14 px dot. */
export const POINT_RADIUS = 7;
/** A drag shorter than this, on screen, places a point rather than a box. */
export const MIN_BOX_PX = 6;
/** Every tag and dot answers within an invisible 44 px target. */
export const HIT_RADIUS = 22;

export const isPoint = (region: ImageRegion): boolean => region.w <= 0 && region.h <= 0;

const clamp = (value: number, low: number, high: number) => Math.min(Math.max(value, low), high);

/**
 * The tag's centre in pixels of a `size` frame, and the side it took. A spot that
 * would overlap a tag already at one of `avoid`'s centres or one of `marks` is
 * passed over while another spot fits; tags weigh more than marks.
 */
export function placeTag(region: ImageRegion, size: Size, radius = TAG_RADIUS, gap = TAG_GAP, avoid: Point[] = [], marks: Mark[] = []): Point & { side: TagSide } {
	const { width, height } = size;
	const r = radius + 2;
	const x0 = region.x * width;
	const y0 = region.y * height;
	const fits = (cx: number, cy: number) => r <= cx && cx <= width - r && r <= cy && cy <= height - r;
	let spots: [number, number, TagSide][];
	if (isPoint(region)) {
		const reach = radius * 1.4;
		spots = [[x0 + reach, y0 - reach, 'up-right'], [x0 - reach, y0 - reach, 'up-left'], [x0 + reach, y0 + reach, 'down-right'], [x0 - reach, y0 + reach, 'down-left']];
	} else {
		const x1 = x0 + region.w * width;
		spots = [[x0 - gap - r, y0 + r, 'left'], [x0 + r, y0 - gap - r, 'above'], [x1 + gap + r, y0 + r, 'right']];
	}
	const clear = (cx: number, cy: number) => avoid.every((other) => Math.hypot(cx - other.x, cy - other.y) >= 2 * r);
	const open = (cx: number, cy: number) => marks.every((mark) => 'r' in mark
		? Math.hypot(cx - mark.x, cy - mark.y) >= r + mark.r
		: Math.hypot(cx - clamp(cx, mark.x0, mark.x1), cy - clamp(cy, mark.y0, mark.y1)) >= r);
	for (const [x, y, side] of spots) if (fits(x, y) && clear(x, y) && open(x, y)) return { x, y, side };
	for (const [x, y, side] of spots) if (fits(x, y) && clear(x, y)) return { x, y, side };
	for (const [x, y, side] of spots) if (fits(x, y)) return { x, y, side };
	const [x, y, side]: [number, number, TagSide] = isPoint(region) ? spots[0] : [x0 + gap + radius, y0 + gap + radius, 'inside'];
	// A tag never leaves the image, even on one smaller than the tag itself.
	return {
		x: clamp(x, Math.min(r, width / 2), Math.max(width - r, width / 2)),
		y: clamp(y, Math.min(r, height / 2), Math.max(height - r, height / 2)),
		side,
	};
}

/**
 * Every tag on one image, placed in `regions`' order, each keeping clear of the
 * tags before it and of every other comment's mark; `dot` is a point's dot
 * radius with its ring.
 */
export function placeTags(regions: ImageRegion[], size: Size, radius = TAG_RADIUS, gap = TAG_GAP, dot = POINT_RADIUS + 2): (Point & { side: TagSide })[] {
	const { width, height } = size;
	const marks: Mark[] = regions.map((region) => isPoint(region)
		? { x: region.x * width, y: region.y * height, r: dot }
		: { x0: region.x * width, y0: region.y * height, x1: (region.x + region.w) * width, y1: (region.y + region.h) * height });
	const placed: (Point & { side: TagSide })[] = [];
	regions.forEach((region, index) => placed.push(placeTag(region, size, radius, gap, placed, marks.filter((_, other) => other !== index))));
	return placed;
}

/** The region a drag from `a` to `b` (pixels in a `size` frame) draws; a short drag is a point at `a`. */
export function regionFromDrag(a: Point, b: Point, size: Size): ImageRegion {
	const { width, height } = size;
	const ax = clamp(a.x, 0, width), ay = clamp(a.y, 0, height);
	const bx = clamp(b.x, 0, width), by = clamp(b.y, 0, height);
	const round = (value: number) => Math.round(value * 10000) / 10000;
	if (Math.abs(bx - ax) < MIN_BOX_PX && Math.abs(by - ay) < MIN_BOX_PX) {
		return { x: round(ax / width), y: round(ay / height), w: 0, h: 0 };
	}
	const x = Math.min(ax, bx) / width, y = Math.min(ay, by) / height;
	return {
		x: round(x), y: round(y),
		w: round(Math.min(Math.abs(bx - ax) / width, 1 - x)),
		h: round(Math.min(Math.abs(by - ay) / height, 1 - y)),
	};
}

/** Where a region sits on screen, for a frame drawn at `frame`; a point is its dot. */
export function regionRect(region: ImageRegion, frame: DOMRect): DOMRect {
	const left = frame.left + region.x * frame.width;
	const top = frame.top + region.y * frame.height;
	if (isPoint(region)) return new DOMRect(left - POINT_RADIUS, top - POINT_RADIUS, POINT_RADIUS * 2, POINT_RADIUS * 2);
	return new DOMRect(left, top, region.w * frame.width, region.h * frame.height);
}

/** The middle of a region and its tag together, as a fraction of the image, so a glide to it lands the tag clear of the edge too. */
export function revealCentre(region: ImageRegion, tag: DOMRect | null, frame: DOMRect): Point {
	const box = regionRect(region, frame);
	const left = Math.min(box.left, tag?.left ?? box.left), right = Math.max(box.right, tag?.right ?? box.right);
	const top = Math.min(box.top, tag?.top ?? box.top), bottom = Math.max(box.bottom, tag?.bottom ?? box.bottom);
	return { x: ((left + right) / 2 - frame.left) / frame.width, y: ((top + bottom) / 2 - frame.top) / frame.height };
}

const intersects = (a: DOMRect, b: DOMRect) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

/**
 * The comments whose agent name, beside its tag in `layer`, covers another
 * tag on screen or another comment's point, so the name hides at rest.
 */
export function crowdedNames(layer: HTMLElement): Set<string> {
	const tagsOnScreen = [...layer.querySelectorAll<HTMLElement>('[data-region-tag]')].filter((tag) => !tag.inert);
	const dots = [...layer.querySelectorAll<HTMLElement>('[data-region-dot]')];
	const hidden = new Set<string>();
	for (const tag of tagsOnScreen) {
		const name = tag.querySelector<HTMLElement>('.name');
		const id = tag.dataset.commentId;
		if (!name || !id) continue;
		const nameRect = name.getBoundingClientRect();
		const covers = (other: HTMLElement) => intersects(nameRect, other.getBoundingClientRect());
		if (tagsOnScreen.some((other) => other !== tag && covers(other)) || dots.some((dot) => dot.dataset.regionDot !== id && covers(dot))) hidden.add(id);
	}
	return hidden;
}

/** The target nearest `at` within the 44 px hit area, so overlapping targets go to the closer centre. */
export function nearestTarget<T extends Point & { id: string }>(targets: T[], at: Point, reach = HIT_RADIUS): string | null {
	let best: { id: string; d: number } | null = null;
	for (const target of targets) {
		const d = Math.hypot(target.x - at.x, target.y - at.y);
		if (d <= reach && (!best || d < best.d)) best = { id: target.id, d };
	}
	return best?.id ?? null;
}

/** The author's colour: an agent's own, or for a person the accent of the image's project. */
export function regionColor(comment: { authorKind?: 'user' | 'agent'; authorThreadId?: string | null }, projectId: string | null, accent?: string | null): string {
	if (comment.authorKind === 'agent' && comment.authorThreadId) return agentColor(comment.authorThreadId);
	return projectAccent(projectId, accent);
}

const pct = (value: number) => `${Math.round(value * 100)}%`;

/** A short spoken description: "box 30% wide, 10% tall at 10% across, 20% down". */
export function describeRegion(region: ImageRegion): string {
	const at = `${pct(region.x)} across, ${pct(region.y)} down`;
	return isPoint(region) ? `point at ${at}` : `box ${pct(region.w)} wide, ${pct(region.h)} tall at ${at}`;
}
