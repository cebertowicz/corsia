import { vi } from 'vitest';

/**
 * Gives a component that measures its own canvas a controlled width in jsdom,
 * which has no layout. `ChatArtifactSplit` reads `getBoundingClientRect` once and
 * then any resize entry, so both paths are answered with the same box; product code
 * is never touched or instrumented.
 */

// artifactSplitLayout.COMBINED_MIN = CHAT_MIN 420 + ARTIFACT_MIN 360 + DIVIDER_WIDTH 8.
// Compact is `0 < canvasWidth < COMBINED_MIN`, so this is the only difference between
// the compact and wide cases under test.
export const COMBINED_MIN = 788;
export const COMPACT_CANVAS = 412;
export const WIDE_CANVAS = 1440;

/** What this DOM really provides, recorded once for the environment report. */
export const jsdomFacts = { resizeObserver: typeof ResizeObserver !== 'undefined' };

function box(width: number): DOMRect {
	return {
		x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0, width, height: 0,
		toJSON: () => ({}),
	} as unknown as DOMRect;
}

/** Measures every canvas in the tree at `width`, except the one the component measures. */
export function measureCanvasAs(width: number): void {
	vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function measured(this: Element) {
		return box(this.classList.contains('split') ? width : 0);
	});
	vi.stubGlobal('ResizeObserver', class HarnessResizeObserver {
		private readonly onResize: ResizeObserverCallback;

		constructor(callback: ResizeObserverCallback) { this.onResize = callback; }

		observe(target: Element): void {
			const entry = {
				contentRect: box(target.classList.contains('split') ? width : 0),
				target,
				contentBoxSize: [{ inlineSize: width, blockSize: 0 }],
				borderBoxSize: [{ inlineSize: width, blockSize: 0 }],
				devicePixelContentBoxSize: [{ inlineSize: width, blockSize: 0 }],
			} as unknown as ResizeObserverEntry;
			this.onResize([entry], this as unknown as ResizeObserver);
		}

		unobserve(): void {}

		disconnect(): void {}
	} as unknown as typeof ResizeObserver);
}
