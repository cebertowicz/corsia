import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnimationFrameCoalescer, clampPoint, drawShifted, FrameScrollShift, SCROLL_CONFIRM_MS } from './browserPane';

const frames: FrameRequestCallback[] = [];
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
	frames.push(callback);
	return frames.length;
});
vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames[id - 1] = () => {}; });

afterEach(() => { frames.length = 0; vi.restoreAllMocks(); vi.useRealTimers(); });

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

	it('drops a prediction no frame confirms, so a wheel an inner scroller took cannot leave the page shifted', () => {
		vi.useFakeTimers();
		const expired = vi.fn();
		const scroll = new FrameScrollShift(expired);
		scroll.replace({ scrollOffsetY: 100, scrollMaxY: 600 });
		scroll.shift({ x: 0, y: 120 }, 0);
		// The inner scroller moved, so frames keep arriving with the page where it was.
		scroll.replace({ scrollOffsetY: 100, scrollMaxY: 600 });
		expect(scroll.translation.y).toBe(120);
		vi.advanceTimersByTime(SCROLL_CONFIRM_MS);
		expect(expired).toHaveBeenCalledOnce();
		expect(scroll.translation.y).toBe(0);
		// The rest of that gesture is not predicted; a new one after a pause is.
		scroll.shift({ x: 0, y: 40 }, 100);
		expect(scroll.translation.y).toBe(0);
		scroll.shift({ x: 0, y: 40 }, 100 + SCROLL_CONFIRM_MS);
		expect(scroll.translation.y).toBe(40);
	});

	it('restarts the wait while frames show the page moving', () => {
		vi.useFakeTimers();
		const expired = vi.fn();
		const scroll = new FrameScrollShift(expired);
		scroll.replace({ scrollOffsetY: 100, scrollMaxY: 600 });
		scroll.shift({ x: 0, y: 180 }, 0);
		vi.advanceTimersByTime(SCROLL_CONFIRM_MS - 50);
		scroll.replace({ scrollOffsetY: 180, scrollMaxY: 600 });
		vi.advanceTimersByTime(SCROLL_CONFIRM_MS - 50);
		expect(expired).not.toHaveBeenCalled();
		expect(scroll.translation.y).toBe(100);
		scroll.replace({ scrollOffsetY: 280, scrollMaxY: 600 });
		vi.advanceTimersByTime(SCROLL_CONFIRM_MS);
		expect(expired).not.toHaveBeenCalled();
		expect(scroll.translation.y).toBe(0);
	});

	it('fills the band a predicted scroll uncovers with the frame edge colour, not stretched pixels', () => {
		const probe = { imageSmoothingQuality: 'low', drawImage: vi.fn() };
		vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(probe as never);
		const smoothing: boolean[] = [];
		const context = {
			imageSmoothingEnabled: true, save: vi.fn(), restore: vi.fn(), beginPath: vi.fn(), rect: vi.fn(), clip: vi.fn(),
			drawImage: vi.fn(function (this: { imageSmoothingEnabled: boolean }) { smoothing.push(this.imageSmoothingEnabled); })
		};
		const frame = new Image();
		drawShifted(context as never, frame, 400, 800, 10, 20, 200, 400, { x: 0, y: 60 }, 0.5);
		expect(probe.drawImage).toHaveBeenCalledWith(frame, 0, 799, 400, 1, 0, 0, 1, 1);
		expect(probe.imageSmoothingQuality).toBe('high');
		expect(context.drawImage.mock.calls).toEqual([
			[frame, 0, 0, 400, 800, 10, -10, 200, 400],
			[expect.any(HTMLCanvasElement), 0, 0, 1, 1, 10, 390, 200, 30]
		]);
		// The band is the one blended edge pixel stretched flat, never smoothed into a gradient.
		expect(smoothing).toEqual([true, false]);
		expect(context.restore).toHaveBeenCalledOnce();
	});
});
