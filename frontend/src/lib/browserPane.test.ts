import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnimationFrameCoalescer, clampPoint, FrameScrollShift } from './browserPane';

const frames: FrameRequestCallback[] = [];
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
	frames.push(callback);
	return frames.length;
});
vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames[id - 1] = () => {}; });

afterEach(() => { frames.length = 0; vi.restoreAllMocks(); });

describe('browser pane input helpers', () => {
	it('clamps releases to the remote page edge', () => {
		expect(clampPoint({ x: -2, y: 120 }, 100, 80)).toEqual({ x: 0, y: 80 });
	});

	it('sends only the newest position in an animation frame', () => {
		const sent: number[] = [];
		const coalescer = new AnimationFrameCoalescer((value: number) => sent.push(value));
		coalescer.push(1);
		coalescer.push(2);
		expect(sent).toEqual([]);
		frames.shift()!(0);
		expect(sent).toEqual([2]);
	});

	it('shifts the shown frame immediately and replaces the prediction with Chrome metadata', () => {
		const scroll = new FrameScrollShift();
		scroll.replace({ scrollOffsetX: 20, scrollOffsetY: 100, scrollMaxX: 40, scrollMaxY: 250 });
		scroll.shift({ x: 80, y: 90 });
		expect(scroll.translation).toEqual({ x: 20, y: 90 });
		scroll.replace({ scrollOffsetX: 40, scrollOffsetY: 190, scrollMaxX: 40, scrollMaxY: 250 });
		expect(scroll.translation).toEqual({ x: 0, y: 0 });
	});

	it('keeps the wheel delta that Chrome has not applied when an intermediate frame arrives', () => {
		const scroll = new FrameScrollShift();
		scroll.replace({ scrollOffsetY: 100, scrollMaxY: 600 });
		scroll.shift({ x: 0, y: 180 });
		scroll.replace({ scrollOffsetY: 180, scrollMaxY: 600 });
		expect(scroll.translation).toEqual({ x: 0, y: 100 });
		scroll.replace({ scrollOffsetY: 180, scrollMaxY: 600 });
		expect(scroll.translation.y).toBe(100);
		scroll.replace({ scrollOffsetY: 280, scrollMaxY: 600 });
		expect(scroll.translation.y).toBe(0);
	});

	it('follows real frames with no pending input, consumes overshoot and clamps reverse scrolling', () => {
		const scroll = new FrameScrollShift();
		scroll.replace({ scrollOffsetY: 100, scrollMaxY: 600 });
		expect(scroll.translation.y).toBe(0);
		scroll.shift({x: 0, y: 50});
		scroll.replace({scrollOffsetY: 170, scrollMaxY: 600});
		expect(scroll.translation.y).toBe(0);
		scroll.shift({x: 0, y: -200});
		expect(scroll.translation.y).toBe(-170);
		scroll.replace({scrollOffsetY: 80, scrollMaxY: 600});
		expect(scroll.translation.y).toBe(-80);
		scroll.replace({scrollOffsetY: 0, scrollMaxY: 600});
		expect(scroll.translation.y).toBe(0);
	});
});
