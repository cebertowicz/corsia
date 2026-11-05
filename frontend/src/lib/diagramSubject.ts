import type { ImageRegion } from '$lib/imageRegions';

/** Diagram elements in the same normalized coordinates as an image comment. */
export function diagramElements(svg: SVGSVGElement): ImageRegion[] {
	const view = svg.viewBox.baseVal;
	const matrix = svg.getScreenCTM();
	if (!matrix || !view.width || !view.height) return [];
	const inverse = matrix.inverse();
	return [...svg.querySelectorAll('g.node, g.edgeLabel, .cluster-label, text, foreignObject, .actor, .messageText, .labelBox')]
		.map((element) => {
			const rect = element.getBoundingClientRect();
			if (!rect.width || !rect.height) return null;
			const corners = [new DOMPoint(rect.left, rect.top), new DOMPoint(rect.right, rect.top), new DOMPoint(rect.left, rect.bottom), new DOMPoint(rect.right, rect.bottom)].map((point) => point.matrixTransform(inverse));
			const xs = corners.map((point) => point.x), ys = corners.map((point) => point.y);
			const left = Math.min(...xs), right = Math.max(...xs), top = Math.min(...ys), bottom = Math.max(...ys);
			return { x: (left - view.x) / view.width, y: (top - view.y) / view.height, w: (right - left) / view.width, h: (bottom - top) / view.height };
		}).filter((region): region is ImageRegion => region !== null);
}

/** The selection and each diagram element it directly overlaps, without expanding transitively. */
export function subjectForRegion(box: ImageRegion, elements: ImageRegion[]): ImageRegion {
	let left = box.x, top = box.y, right = box.x + box.w, bottom = box.y + box.h;
	for (const element of elements) {
		if (element.x > box.x + box.w || element.x + element.w < box.x || element.y > box.y + box.h || element.y + element.h < box.y) continue;
		left = Math.min(left, element.x); top = Math.min(top, element.y);
		right = Math.max(right, element.x + element.w); bottom = Math.max(bottom, element.y + element.h);
	}
	return { x: left, y: top, w: right - left, h: bottom - top };
}
