import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnimationFrameCoalescer, clampPoint } from './browserPane';

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
});
