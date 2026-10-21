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

/**
 * How long a predicted scroll waits for a real frame that moves the page before it is dropped, in ms.
 * Above the slowest run's 95th-percentile wheel-to-frame time on a busy host (619 ms, Oct 20), so an ordinary
 * scroll is not snapped back before Chrome shows it.
 */
export const SCROLL_CONFIRM_MS = 700;

const edgeSamples = new WeakMap<HTMLImageElement, Map<string, HTMLCanvasElement | null>>();

/** One edge row or column blended into a single pixel, so an uncovered band reads as page background. Made once per frame. */
function edgeSample(source: HTMLImageElement, sx: number, sy: number, sw: number, sh: number): HTMLCanvasElement | null {
	const key = `${sx},${sy},${sw},${sh}`;
	const known = edgeSamples.get(source) ?? new Map<string, HTMLCanvasElement | null>();
	edgeSamples.set(source, known);
	if (!known.has(key)) known.set(key, sampleEdge(source, sx, sy, sw, sh));
	return known.get(key) ?? null;
}

function sampleEdge(source: HTMLImageElement, sx: number, sy: number, sw: number, sh: number): HTMLCanvasElement | null {
	const probe = document.createElement('canvas');
	probe.width = probe.height = 1;
	const context = probe.getContext('2d');
	if (!context) return null;
	// High-quality downscaling blends the whole row into the one pixel instead of sampling its middle.
	context.imageSmoothingQuality = 'high';
	context.drawImage(source, sx, sy, sw, sh, 0, 0, 1, 1);
	return probe;
}

/** Draws the frame moved by the predicted scroll; the band it uncovers takes the colour of the frame's edge. */
export function drawShifted(context: CanvasRenderingContext2D, source: HTMLImageElement, sourceWidth: number, sourceHeight: number, left: number, top: number, drawWidth: number, drawHeight: number, shift: PanePoint, scale: number): void {
	context.save();
	context.beginPath();
	context.rect(left, top, drawWidth, drawHeight);
	context.clip();
	context.drawImage(source, 0, 0, sourceWidth, sourceHeight, left - shift.x * scale, top - shift.y * scale, drawWidth, drawHeight);
	const horizontal = Math.min(drawWidth, Math.abs(shift.x * scale));
	const vertical = Math.min(drawHeight, Math.abs(shift.y * scale));
	// The one-pixel sample stretched without smoothing is a flat fill of the page's own edge colour.
	context.imageSmoothingEnabled = false;
	if (horizontal) {
		const sample = edgeSample(source, shift.x > 0 ? sourceWidth - 1 : 0, 0, 1, sourceHeight);
		if (sample) context.drawImage(sample, 0, 0, 1, 1, shift.x > 0 ? left + drawWidth - horizontal : left, top, horizontal, drawHeight);
	}
	if (vertical) {
		const sample = edgeSample(source, 0, shift.y > 0 ? sourceHeight - 1 : 0, sourceWidth, 1);
		if (sample) context.drawImage(sample, 0, 0, 1, 1, left, shift.y > 0 ? top + drawHeight - vertical : top, drawWidth, vertical);
	}
	context.restore();
}

/**
 * Predicts the viewport's scroll position until a screencast frame confirms its real offset.
 * A prediction no frame confirms within SCROLL_CONFIRM_MS (an inner scroller or the page took the wheel) is
 * dropped, and the rest of that gesture is not predicted; `onExpire` redraws the unshifted frame.
 */
export class FrameScrollShift {
	private frame = { x: 0, y: 0, maxX: Infinity, maxY: Infinity };
	private shown = { x: 0, y: 0 };
	private lastShift = -Infinity;
	private muted = false;
	private timer: ReturnType<typeof setTimeout> | undefined;

	constructor(private readonly onExpire: () => void = () => {}) {}

	replace(metadata: ScrollFrame): void {
		const previous = this.frame;
		const x = typeof metadata.scrollOffsetX === 'number' ? metadata.scrollOffsetX : previous.x;
		const y = typeof metadata.scrollOffsetY === 'number' ? metadata.scrollOffsetY : previous.y;
		// The range is measured only when capture starts, so content that grew since can put the real offset past it;
		// the real offset is never clamped, only the prediction beyond it.
		const maxX = typeof metadata.scrollMaxX === 'number' ? Math.max(0, metadata.scrollMaxX, x) : Infinity;
		const maxY = typeof metadata.scrollMaxY === 'number' ? Math.max(0, metadata.scrollMaxY, y) : Infinity;
		// A move against the prediction (a navigation or a script jumped the page) means Chrome did not apply it.
		const remaining = (prediction: number, advance: number) => {
			if (prediction === 0 || Math.sign(advance) === -Math.sign(prediction)) return 0;
			return prediction > 0 ? Math.max(0, prediction - advance) : Math.min(0, prediction - advance);
		};
		const pending = {
			x: remaining(this.shown.x - previous.x, x - previous.x),
			y: remaining(this.shown.y - previous.y, y - previous.y)
		};
		this.frame = { x, y, maxX, maxY };
		this.shown = { x: Math.min(maxX, Math.max(0, x + pending.x)), y: Math.min(maxY, Math.max(0, y + pending.y)) };
		if (this.shown.x === x && this.shown.y === y) this.settle();
		else if (x !== previous.x || y !== previous.y) this.wait();
	}

	shift(delta: PanePoint, now = performance.now()): void {
		// A pause between wheel movements starts a new gesture, which is predicted again.
		if (now - this.lastShift >= SCROLL_CONFIRM_MS) this.muted = false;
		this.lastShift = now;
		if (this.muted) return;
		this.shown = {
			x: Math.min(this.frame.maxX, Math.max(0, this.shown.x + delta.x)),
			y: Math.min(this.frame.maxY, Math.max(0, this.shown.y + delta.y))
		};
		if (this.timer === undefined && (this.shown.x !== this.frame.x || this.shown.y !== this.frame.y)) this.wait();
	}

	get translation(): PanePoint {
		return { x: this.shown.x - this.frame.x, y: this.shown.y - this.frame.y };
	}

	dispose(): void {
		this.settle();
	}

	/** (Re)starts the wait for a frame that moves the page. */
	private wait(): void {
		clearTimeout(this.timer);
		this.timer = setTimeout(() => this.expire(), SCROLL_CONFIRM_MS);
	}

	private settle(): void {
		clearTimeout(this.timer);
		this.timer = undefined;
	}

	private expire(): void {
		this.settle();
		this.muted = true;
		this.shown = { x: this.frame.x, y: this.frame.y };
		this.onExpire();
	}
}
