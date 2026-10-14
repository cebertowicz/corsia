import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnimationFrameCoalescer, clampPoint, LatestFramePipeline } from './browserPane';

const frames: FrameRequestCallback[] = [];
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
	frames.push(callback);
	return frames.length;
});
vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames[id - 1] = () => {}; });

afterEach(() => { frames.length = 0; vi.restoreAllMocks(); });

describe('browser pane input and frame pipeline', () => {
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

	it('draws the newest decoded bitmap without clearing an earlier bitmap first', async () => {
		const closed: string[] = [];
		const bitmaps = [
			{ width: 100, height: 100, close: () => closed.push('one') },
			{ width: 100, height: 100, close: () => closed.push('two') },
		];
		vi.stubGlobal('fetch', vi.fn(async () => ({ blob: async () => new Blob() })));
		vi.stubGlobal('createImageBitmap', vi.fn(async () => bitmaps.shift()));
		const drawn: string[] = [];
		const pipeline = new LatestFramePipeline((_bitmap, frame) => drawn.push(frame.data));
		pipeline.offer({ data: 'old', width: 100, height: 100 });
		pipeline.offer({ data: 'new', width: 100, height: 100 });
		await vi.waitFor(() => expect(frames.length).toBeGreaterThan(0));
		frames.pop()!(0);
		expect(drawn).toEqual(['new']);
		pipeline.dispose();
		expect(closed).toContain('two');
	});
});
