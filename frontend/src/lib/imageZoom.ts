/**
 * Zoom arithmetic for the image viewer. A scale is CSS pixels per image pixel, so 1 is 100%.
 * Kept free of the DOM so every rule is testable; the viewer only reads sizes and applies results.
 */

export const FIT_MARGIN = 24;
export const MAX_SCALE = 8;
export const ZOOM_STEP = 1.25;

export interface Size { width: number; height: number }
export interface Point { x: number; y: number }

/**
 * The scale at which the whole image sits inside the viewport with a margin on every side.
 * Never above 1: a small image is shown at its own size, not stretched.
 */
export function fitScale(image: Size, viewport: Size, margin = FIT_MARGIN): number {
	if (!(image.width > 0) || !(image.height > 0)) return 1;
	const room = { width: Math.max(1, viewport.width - 2 * margin), height: Math.max(1, viewport.height - 2 * margin) };
	return Math.min(1, room.width / image.width, room.height / image.height);
}

/** Zoom stays between Fit and 8x; a tiny image that already fits at 100% cannot be shrunk further. */
export function clampScale(scale: number, fit: number): number {
	return Math.min(MAX_SCALE, Math.max(fit, scale));
}

/** One button step in or out, landing exactly on Fit or 100% when it crosses them so those stay reachable. */
export function stepScale(scale: number, direction: 1 | -1, fit: number): number {
	const next = direction > 0 ? scale * ZOOM_STEP : scale / ZOOM_STEP;
	const snapped = [fit, 1].find((stop) => (direction > 0 ? scale < stop && next > stop : scale > stop && next < stop));
	return clampScale(snapped ?? next, fit);
}

/**
 * The factor a wheel event zooms by. Trackpad pinch arrives as a ctrl-wheel with small deltas and Cmd-scroll with
 * line- or pixel-sized ones, so the delta is capped and the rate is exponential: equal steps in and out cancel.
 */
export function wheelFactor(deltaY: number, deltaMode = 0): number {
	const pixels = deltaMode === 1 ? deltaY * 16 : deltaMode === 2 ? deltaY * 100 : deltaY;
	return Math.exp(-Math.max(-120, Math.min(120, pixels)) * 0.0075);
}

export function distance(a: Point, b: Point): number {
	return Math.hypot(a.x - b.x, a.y - b.y);
}

export function midpoint(a: Point, b: Point): Point {
	return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

/** Where a client point lies on the image as 0..1 fractions of its drawn box; outside the box it is clamped to the edge. */
export function fractionOnImage(client: Point, box: { left: number; top: number; width: number; height: number }): Point {
	const unit = (value: number, origin: number, extent: number) => (extent > 0 ? Math.min(1, Math.max(0, (value - origin) / extent)) : 0.5);
	return { x: unit(client.x, box.left, box.width), y: unit(client.y, box.top, box.height) };
}

/**
 * How far to scroll so the image fraction `at` sits under the client point `to` once the image is drawn in `box`.
 * Zooming around the pointer is: remember the fraction under it, resize, then scroll by this.
 */
export function scrollToHold(at: Point, to: Point, box: { left: number; top: number; width: number; height: number }): Point {
	return { x: box.left + at.x * box.width - to.x, y: box.top + at.y * box.height - to.y };
}

/** Whether a scale is 100%, allowing for the rounding a pinch leaves behind. */
export function isActualSize(scale: number): boolean {
	return Math.abs(scale - 1) < 0.005;
}

export function percentLabel(scale: number): string {
	return `${Math.round(scale * 100)}%`;
}
