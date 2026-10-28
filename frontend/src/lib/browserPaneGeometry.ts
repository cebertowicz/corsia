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
