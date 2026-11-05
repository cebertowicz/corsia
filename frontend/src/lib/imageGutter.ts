import { placeTags, TAG_GAP, TAG_RADIUS, type ImageRegion, type Point, type Size, type TagSide } from './imageRegions';

export type BadgeTag = Point & { side: TagSide | GutterSide; anchor?: Point };

/** The viewer's fixed-size badges, in pixels of its displayed image. */
export function viewerBadgeTags(regions: ImageRegion[], size: Size): BadgeTag[] {
	return badgeTags(regions, size, TAG_RADIUS, TAG_GAP);
}

/** Keep the usual on-image positions; only badges that still overlap move to the edge. */
export function badgeTags(regions: ImageRegion[], size: Size, radius: number, gap: number, dot?: number): BadgeTag[] {
	const positions: BadgeTag[] = regions.map((region) => placeTags([region], size, radius, gap, dot)[0]);
	const inside: Point[] = [];
	const outside: number[] = [];
	positions.forEach((tag, index) => {
		if (inside.some((other) => Math.hypot(tag.x - other.x, tag.y - other.y) < 2 * (radius + 2))) outside.push(index);
		else inside.push(tag);
	});
	const edge = gutterTags(outside.map((index) => regions[index]), size, radius, gap);
	outside.forEach((index, i) => { positions[index] = edge[i]; });
	return positions;
}

/** Python's round-to-even, used by the delivered raster renderer. */
function roundEven(value: number): number {
	const floor = Math.floor(value);
	return value - floor === 0.5 ? floor + (floor % 2) : Math.round(value);
}

/** Use the delivered raster's geometry at every viewer zoom. Discs remain screen-sized. */
export function rasterBadgeTags(regions: ImageRegion[], natural: Size, shown: Size): BadgeTag[] {
	const fit = Math.min(1, 1600 / Math.max(natural.width, natural.height));
	const size = { width: Math.max(1, roundEven(natural.width * fit)), height: Math.max(1, roundEven(natural.height * fit)) };
	const sx = shown.width / size.width, sy = shown.height / size.height;
	return badgeTags(regions, size, TAG_RADIUS, TAG_GAP).map((tag) => ({
		...tag, x: tag.x * sx, y: tag.y * sy,
		...(tag.anchor ? { anchor: { x: tag.anchor.x * sx, y: tag.anchor.y * sy } } : {}),
	}));
}

export type GutterSide = 'left' | 'right' | 'above' | 'below';
export type GutterTag = Point & { side: GutterSide; anchor: Point };

/** Place tags outside the nearest edge, in anchor order to keep neighbouring lines from crossing. */
export function gutterTags(regions: ImageRegion[], size: Size, radius: number, gap: number): GutterTag[] {
	const r = radius + 2;
	const spacing = 2 * r + gap;
	const tags: GutterTag[] = regions.map((region) => {
		const x0 = region.x * size.width, y0 = region.y * size.height;
		const x1 = (region.x + region.w) * size.width, y1 = (region.y + region.h) * size.height;
		const choices: [GutterSide, number][] = [['left', x0], ['right', size.width - x1], ['above', y0], ['below', size.height - y1]];
		const side = choices.reduce((best, candidate) => candidate[1] < best[1] ? candidate : best)[0];
		if (side === 'left') return { x: -gap - r, y: y0, side, anchor: { x: x0, y: y0 } };
		if (side === 'right') return { x: size.width + gap + r, y: y0, side, anchor: { x: x1, y: y0 } };
		if (side === 'above') return { x: x0, y: -gap - r, side, anchor: { x: x0, y: y0 } };
		return { x: x0, y: size.height + gap + r, side, anchor: { x: x0, y: y1 } };
	});
	for (const side of ['left', 'right', 'above', 'below'] as const) {
		const vertical = side === 'left' || side === 'right';
		const extent = vertical ? size.height : size.width;
		const lower = Math.min(r, extent / 2), upper = Math.max(extent - r, extent / 2);
		const entries = tags.filter((tag) => tag.side === side).sort((a, b) => (vertical ? a.y - b.y : a.x - b.x));
		const positions: number[] = [];
		for (const tag of entries) {
			const desired = Math.max(lower, Math.min(upper, vertical ? tag.y : tag.x));
			positions.push(Math.max(desired, positions.length ? positions[positions.length - 1] + spacing : lower));
		}
		for (let i = positions.length - 1; i >= 0; i--) positions[i] = Math.min(positions[i], i === positions.length - 1 ? upper : positions[i + 1] - spacing);
		// If this edge cannot hold every tag, keep them visible and report the overlap in the evidence.
		if (positions.length && positions[0] < lower) {
			positions.forEach((_, i) => positions[i] = lower + (upper - lower) * i / Math.max(1, positions.length - 1));
		}
		entries.forEach((tag, i) => { if (vertical) tag.y = positions[i]; else tag.x = positions[i]; });
	}
	return tags;
}
