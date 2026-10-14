export type PanePoint = { x: number; y: number };

/** Keeps a high-rate stream to the one position the receiver can still act on. */
export class AnimationFrameCoalescer<T> {
	private latest: T | null = null;
	private frame = 0;

	constructor(private readonly send: (value: T) => void) {}

	push(value: T): void {
		this.latest = value;
		if (this.frame) return;
		this.frame = requestAnimationFrame(() => {
			this.frame = 0;
			const next = this.latest;
			this.latest = null;
			if (next !== null) this.send(next);
		});
	}

	cancel(): void {
		if (this.frame) cancelAnimationFrame(this.frame);
		this.frame = 0;
		this.latest = null;
	}
}

export function clampPoint(point: PanePoint, width: number, height: number): PanePoint {
	return { x: Math.min(width, Math.max(0, point.x)), y: Math.min(height, Math.max(0, point.y)) };
}

export type BrowserFrame = { data: string; width: number; height: number };

/**
 * Decodes only the most recent stream frame and keeps the last drawable bitmap on screen.
 * The caller supplies rendering because it owns pane geometry and canvas lifecycle.
 */
export class LatestFramePipeline {
	private queued: BrowserFrame | null = null;
	private decoding = false;
	private bitmap: ImageBitmap | null = null;
	private displayedFrame: BrowserFrame | null = null;
	private scheduled = 0;

	constructor(private readonly draw: (bitmap: ImageBitmap, frame: BrowserFrame) => void) {}

	offer(frame: BrowserFrame): void {
		this.queued = frame;
		if (!this.decoding) void this.decodeLatest();
	}

	private async decodeLatest(): Promise<void> {
		this.decoding = true;
		while (this.queued) {
			const frame = this.queued;
			this.queued = null;
			try {
				const response = await fetch(`data:image/jpeg;base64,${frame.data}`);
				const bitmap = await createImageBitmap(await response.blob());
				// A fresher undecoded frame makes this bitmap obsolete before it reaches the canvas.
				if (this.queued) {
					bitmap.close();
					continue;
				}
				this.bitmap?.close();
				this.bitmap = bitmap;
				this.displayedFrame = frame;
				this.scheduleDraw(frame);
			} catch {
				// Keep the last good frame visible if a malformed frame cannot decode.
			}
		}
		this.decoding = false;
	}

	redraw(): void {
		if (this.bitmap && this.displayedFrame) this.scheduleDraw(this.displayedFrame);
	}

	private scheduleDraw(frame: BrowserFrame): void {
		if (this.scheduled) cancelAnimationFrame(this.scheduled);
		this.scheduled = requestAnimationFrame(() => {
			this.scheduled = 0;
			if (this.bitmap) this.draw(this.bitmap, frame);
		});
	}

	dispose(): void {
		if (this.scheduled) cancelAnimationFrame(this.scheduled);
		this.scheduled = 0;
		this.queued = null;
		this.bitmap?.close();
		this.bitmap = null;
		this.displayedFrame = null;
	}
}
