export type PanePoint = { x: number; y: number };

/** Keeps a high-rate stream to the one position the receiver can still act on. */
export class AnimationFrameCoalescer<T> {
	private latest: T | null = null;
	private frame = 0;

	constructor(private readonly send: (value: T) => void, private readonly merge: (previous: T, next: T) => T = (_previous, next) => next) {}

	push(value: T): void {
		this.latest = this.latest === null ? value : this.merge(this.latest, value);
		if (this.frame) return;
		this.frame = requestAnimationFrame(() => this.flush());
	}

	/** Send continuous input before a discrete event so it cannot overtake the event that follows it. */
	flush(): void {
		if (this.frame) cancelAnimationFrame(this.frame);
		this.frame = 0;
		const next = this.latest;
		this.latest = null;
		if (next !== null) this.send(next);
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

type ScrollFrame = { scrollOffsetX?: unknown; scrollOffsetY?: unknown; scrollMaxX?: unknown; scrollMaxY?: unknown };

/** Predict the viewport's scroll position until the next screencast frame confirms its real offset. */
export function fillShiftedEdges(context: CanvasRenderingContext2D, source: HTMLImageElement, sourceWidth: number, sourceHeight: number, left: number, top: number, drawWidth: number, drawHeight: number, shift: PanePoint, scale: number): void {
	const horizontal = Math.min(drawWidth, Math.abs(shift.x * scale));
	const vertical = Math.min(drawHeight, Math.abs(shift.y * scale));
	if (horizontal) {
		const sourceX = shift.x > 0 ? sourceWidth - 1 : 0;
		const targetX = shift.x > 0 ? left + drawWidth - horizontal : left;
		context.drawImage(source, sourceX, 0, 1, sourceHeight, targetX, top, horizontal, drawHeight);
	}
	if (vertical) {
		const sourceY = shift.y > 0 ? sourceHeight - 1 : 0;
		const targetY = shift.y > 0 ? top + drawHeight - vertical : top;
		context.drawImage(source, 0, sourceY, sourceWidth, 1, left, targetY, drawWidth, vertical);
	}
}

export class FrameScrollShift {
	private frame = { x: 0, y: 0, maxX: Infinity, maxY: Infinity };
	private shown = { x: 0, y: 0 };

	replace(metadata: ScrollFrame): void {
		const previous = this.frame;
		const x = typeof metadata.scrollOffsetX === 'number' ? metadata.scrollOffsetX : previous.x;
		const y = typeof metadata.scrollOffsetY === 'number' ? metadata.scrollOffsetY : previous.y;
		const maxX = typeof metadata.scrollMaxX === 'number' ? Math.max(0, metadata.scrollMaxX) : Infinity;
		const maxY = typeof metadata.scrollMaxY === 'number' ? Math.max(0, metadata.scrollMaxY) : Infinity;
		const remaining = (prediction: number, advance: number) => prediction > 0
			? Math.max(0, prediction - Math.max(0, advance))
			: Math.min(0, prediction - Math.min(0, advance));
		const pending = {
			x: remaining(this.shown.x - previous.x, x - previous.x),
			y: remaining(this.shown.y - previous.y, y - previous.y)
		};
		this.frame = { x, y, maxX, maxY };
		this.shown = { x: Math.min(maxX, Math.max(0, x + pending.x)), y: Math.min(maxY, Math.max(0, y + pending.y)) };
	}

	shift(delta: PanePoint): void {
		this.shown = {
			x: Math.min(this.frame.maxX, Math.max(0, this.shown.x + delta.x)),
			y: Math.min(this.frame.maxY, Math.max(0, this.shown.y + delta.y))
		};
	}

	get translation(): PanePoint {
		return { x: this.shown.x - this.frame.x, y: this.shown.y - this.frame.y };
	}
}
