import { describe, expect, it, vi } from 'vitest';
import type { EditorView } from '@codemirror/view';

const { widgetTarget } = vi.hoisted(() => ({ widgetTarget: vi.fn() }));
vi.mock('./widget-scroll', () => ({ widgetTarget }));
vi.mock('./visible-region', async (actual) => ({ ...(await actual<typeof import('./visible-region')>()), visibleRegion: () => ({ band: { top: 100, bottom: 800 }, scroller: document.createElement('div') }) }));

import { pressSpan } from './comment-marks';

const LINE = 24;
const view = (coords: (pos: number) => { top: number; bottom: number }) =>
	({ state: { doc: { length: 1000 } }, defaultLineHeight: LINE, coordsAtPos: coords }) as unknown as EditorView;
/** A table row or a diagram drawn `top`..`bottom`: one box for every position in it. */
const widgetBox = (top: number, bottom: number) => ({ start: 0, box: () => new DOMRect(0, top, 300, bottom - top), reveal: () => {} });

describe('pressSpan', () => {
	it('keeps a highlight of up to three lines whole, and only the pressed line of a longer one', () => {
		widgetTarget.mockReturnValue(null);
		const lines = view((pos) => ({ top: 400 + Math.floor(pos / 10) * LINE, bottom: 400 + Math.floor(pos / 10) * LINE + LINE }));
		expect(pressSpan(lines, { from: 10, to: 30 }, 15)).toEqual({ top: 424, bottom: 496, height: 72, regionTop: 100 });
		expect(pressSpan(lines, { from: 10, to: 80 }, 45)).toEqual({ top: 496, bottom: 520, height: 24, regionTop: 100 });
	});

	it('keeps a table row of up to three lines whole', () => {
		widgetTarget.mockReturnValue(widgetBox(500, 560));
		expect(pressSpan(view(() => ({ top: 0, bottom: 0 })), { from: 10, to: 30 }, 10)).toEqual({ top: 500, bottom: 560, height: 60, regionTop: 100 });
	});

	it('keeps only the top line of a table row taller than three lines, so a sheet opened from it is not held down by the whole row', () => {
		widgetTarget.mockReturnValue(widgetBox(500, 900));
		expect(pressSpan(view(() => ({ top: 0, bottom: 0 })), { from: 10, to: 30 }, 10)).toEqual({ top: 500, bottom: 500 + LINE, height: LINE, regionTop: 100 });
	});

	it('keeps only the top line of a diagram taller than three lines', () => {
		widgetTarget.mockReturnValue(widgetBox(200, 620));
		expect(pressSpan(view(() => ({ top: 0, bottom: 0 })), { from: 40, to: 40 }, 40)).toEqual({ top: 200, bottom: 200 + LINE, height: LINE, regionTop: 100 });
	});
});
