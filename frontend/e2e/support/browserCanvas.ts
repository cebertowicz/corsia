import type { Locator } from '@playwright/test';

// Frames are JPEG, so a drawn color is matched within a small tolerance.
/** The shared browser canvas's color at fractions (fx, fy) of its size: `rgb` itself when within 12 per channel. */
export async function colorAt(canvas: Locator, fx: number, fy: number, rgb: number[]) {
	const pixel = await canvas.evaluate((node, [fx, fy]) => {
		const frame = node as HTMLCanvasElement;
		return Array.from(frame.getContext('2d')!.getImageData(Math.floor(frame.width * fx), Math.floor(frame.height * fy), 1, 1).data.slice(0, 3));
	}, [fx, fy]);
	return pixel.every((value, index) => Math.abs(value - rgb[index]) <= 12) ? rgb : pixel;
}
