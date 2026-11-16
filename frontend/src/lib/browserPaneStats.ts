/**
 * Bucket upper bounds in ms. The same list as the backend's HTTP timing histogram
 * (`backend/infra/request_timing.py`), so the server adds the pane's counts to its own and reads
 * both with one percentile estimate.
 */
export const STATS_BUCKET_UPPERS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 60000] as const;

const REPORT_MS = 10_000;
// An input is forgotten if no frame ever names it; this bounds what a stalled page can pile up.
const MAX_PENDING_INPUT = 64;

type Frame = { seq?: unknown; input?: unknown };

function bucketIndex(ms: number): number {
	const index = STATS_BUCKET_UPPERS.findIndex(upper => ms <= upper);
	return index === -1 ? STATS_BUCKET_UPPERS.length - 1 : index;
}

const emptyBuckets = () => STATS_BUCKET_UPPERS.map(() => 0);

/**
 * What this pane measures about its own stream, reported to the server every few seconds: frames
 * painted, the newest frame number painted, how long each took from receipt to paint, how long the
 * oldest input still waiting took to show in the first painted frame that answers it, and how much
 * input was still queued in the socket. Only
 * counts and times; never page content, a position or a key.
 */
export class BrowserPaneStats {
	private nextId = 1;
	private sentAt = new Map<number, number>();
	private painted = 0;
	private paintedSeq = 0;
	private carried = 0;
	// Counts connections: a decode that finishes after `stop()` belongs to the old connection and is ignored.
	private connection = 0;
	private bufferedMax = 0;
	private decode = emptyBuckets();
	private delay = emptyBuckets();
	private timer: ReturnType<typeof setInterval> | undefined;

	constructor(
		private readonly buffered: () => number,
		private readonly now: () => number = () => performance.now(),
		private readonly visible: () => boolean = () => document.visibilityState === 'visible'
	) {}

	/** Gives an input that can change the picture a number, so a later frame can name it. A pointer move is not numbered. */
	stamp<T extends object>(message: T): T {
		this.bufferedMax = Math.max(this.bufferedMax, this.buffered());
		const { type, eventType } = message as { type?: unknown; eventType?: unknown };
		if (type !== 'input_keyboard' && !(type === 'input_mouse' && eventType !== 'mouseMoved')) return message;
		const id = this.nextId++;
		this.sentAt.set(id, this.now());
		if (this.sentAt.size > MAX_PENDING_INPUT) this.sentAt.delete(this.sentAt.keys().next().value as number);
		return { ...message, id };
	}

	/** Call when a frame arrives; call the result with whether it was painted or replaced by a newer one first. */
	arrived(frame: Frame): (drawn: boolean) => void {
		const started = this.now();
		const input = typeof frame.input === 'number' ? frame.input : 0;
		const connection = this.connection;
		return drawn => {
			if (connection !== this.connection) return;
			// A replaced frame still carries its input's name, which the next painted frame answers for.
			if (!drawn) {
				this.carried = Math.max(this.carried, input);
				return;
			}
			this.painted += 1;
			if (typeof frame.seq === 'number') this.paintedSeq = Math.max(this.paintedSeq, frame.seq);
			this.decode[bucketIndex(this.now() - started)] += 1;
			const named = Math.max(input, this.carried);
			this.carried = 0;
			// The oldest input this paint answers sets the delay: under rapid input a later one must not hide how long the first waited.
			let oldest: number | undefined;
			for (const [id, sent] of this.sentAt) {
				if (id > named) break;
				oldest ??= sent;
				this.sentAt.delete(id);
			}
			if (oldest !== undefined) this.delay[bucketIndex(this.now() - oldest)] += 1;
		};
	}

	/** The figures since the last report; the counters start again, the newest painted number does not. */
	report(): Record<string, unknown> {
		const message = {
			type: 'pane_stats', painted: this.painted, painted_seq: this.paintedSeq,
			decode_ms: this.decode, input_to_paint_ms: this.delay,
			up_buffered_max: Math.round(Math.max(this.bufferedMax, this.buffered())), visible: this.visible()
		};
		this.painted = 0;
		this.bufferedMax = 0;
		this.decode = emptyBuckets();
		this.delay = emptyBuckets();
		return message;
	}

	start(send: (message: Record<string, unknown>) => void): void {
		clearInterval(this.timer);
		this.timer = setInterval(() => send(this.report()), REPORT_MS);
	}

	/** Ends the interval and forgets everything: a new connection numbers its frames and inputs from one. */
	stop(): void {
		clearInterval(this.timer);
		this.timer = undefined;
		this.connection += 1;
		this.nextId = 1;
		this.sentAt.clear();
		this.paintedSeq = 0;
		this.carried = 0;
		this.report();
	}
}
