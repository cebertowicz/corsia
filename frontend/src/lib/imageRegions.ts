/**
 * Geometry for comments on part of an image (DCOL-FR13.3, FR13.4).
 *
 * A region is a box or a point in fractions of the image, origin top-left, so it
 * holds at any zoom. Tags follow the same rule as the marked-up copy an agent
 * gets back (`backend/doceditor/image_markup.py` `place_tag`): a tag never covers
 * what its box points at. A box's tag tries left of its top-left corner, above
 * it, then right of the box, and only when the image edge clips all three sits
 * inside the box's corner. A point's tag tries up-right, then the other diagonals.
 */
import type { ImageRegion } from '$lib/api/docs';
import { agentColor } from '$lib/docPresence';
import { projectAccent } from '$lib/utils/projectColors';

export type { ImageRegion };
export type Point = { x: number; y: number };
export type Size = { width: number; height: number };
export type TagSide = 'left' | 'above' | 'right' | 'inside' | 'up-right' | 'up-left' | 'down-right' | 'down-left';

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

/** The tag's centre in pixels of a `size` frame, and the side it took. */
export function placeTag(region: ImageRegion, size: Size, radius = TAG_RADIUS, gap = TAG_GAP): Point & { side: TagSide } {
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
	for (const [x, y, side] of spots) if (fits(x, y)) return { x, y, side };
	const [x, y, side]: [number, number, TagSide] = isPoint(region) ? spots[0] : [x0 + gap + radius, y0 + gap + radius, 'inside'];
	// A tag never leaves the image, even on one smaller than the tag itself.
	return {
		x: clamp(x, Math.min(r, width / 2), Math.max(width - r, width / 2)),
		y: clamp(y, Math.min(r, height / 2), Math.max(height - r, height / 2)),
		side,
	};
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
