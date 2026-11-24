import { clampPoint } from './browserPane';

// Where the page sits in the canvas: drawing and input share it, so a click lands where it is drawn.
export function panePlacement(canvas: HTMLCanvasElement, width: number, height: number) {
	const bounds = canvas.getBoundingClientRect();
	const scale = Math.min(bounds.width / width, bounds.height / height);
	return { bounds, scale, left: (bounds.width - width * scale) / 2, top: (bounds.height - height * scale) / 2 };
}

export function paneCoordinates(event: PointerEvent | WheelEvent, canvas: HTMLCanvasElement, width: number, height: number, clamp = false) {
	const { bounds, scale, left, top } = panePlacement(canvas, width, height);
	const point = { x: (event.clientX - bounds.left - left) / scale, y: (event.clientY - bounds.top - top) / scale };
	return clamp ? clampPoint(point, width, height) : point.x >= 0 && point.y >= 0 && point.x <= width && point.y <= height ? point : null;
}

export type PaneSize = { width: number; height: number };

export function paneSize(canvas: HTMLCanvasElement): PaneSize {
	const bounds = canvas.getBoundingClientRect();
	return { width: Math.round(bounds.width), height: Math.round(bounds.height) };
}

/** The size, pixel density and theme the pane asks the page to take, or null while the canvas has no size. `size` overrides the canvas's current one. */
export function paneAppearance(canvas: HTMLCanvasElement, scheme: string, size: PaneSize = paneSize(canvas)) {
	if (size.width < 1 || size.height < 1) return null;
	const touch = window.matchMedia?.('(pointer: coarse)').matches === true;
	return { type: 'appearance', ...size, scale: devicePixelRatio, scheme, ...(touch ? { touch: true } : {}) };
}
