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
