import type { SelectOutcome } from './cm/typed-reveal-arrival';

export const REVEAL_BASE = 300;
export const REVEAL_PER_CHAR = 8;
export const REVEAL_CAP = 2_500;
export const REDUCED_HIGHLIGHT_FOR = 1_500;

export function revealDelay(chars: number): number {
	return Math.min(REVEAL_CAP, REVEAL_BASE + Math.max(0, chars) * REVEAL_PER_CHAR);
}

interface PreparedArrival<T> {
	value: T;
	chars: number;
	/** Total display time before the reveal when the queue holds it itself; replaces the default scaled by `chars`. */
	delayMs?: number;
}

interface QueueEntry<T> {
	value: T;
	prepared?: PreparedArrival<T>;
}

interface ArrivalQueueOptions<T> {
	isViewing: () => boolean;
	reducedMotion: () => boolean;
	prepare: (value: T) => Promise<PreparedArrival<T>>;
	/**
	 * Start the arrival's select step in an editor and return its end, or null when no editor can run one.
	 * While it runs it is the only clock: the queue waits on it and keeps no delay of its own.
	 */
	select?: (value: T) => Promise<SelectOutcome> | null;
	/** `selecting` is true when an editor draws the highlight itself. */
	highlight: (value: T, selecting: boolean) => void | Promise<void>;
	reveal: (value: T) => void | Promise<void>;
	clear: (value: T) => void | Promise<void>;
	/** The queue stopped because the document is no longer viewed; these arrivals wait for it to return. */
	paused?: (waiting: T[]) => void;
	sleep?: (ms: number) => Promise<void>;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class ArrivalQueue<T> {
	private readonly rows: QueueEntry<T>[] = [];
	private running: Promise<void> | null = null;
	private paused = false;
	private drain = false;
	private skipWait: (() => void) | null = null;

	constructor(private readonly options: ArrivalQueueOptions<T>) {}

	get pending(): number { return this.rows.length; }

	enqueue(value: T): void { this.rows.push({ value }); }

	run(): Promise<void> {
		if (this.running) return this.running;
		this.running = this.pump().finally(() => { this.running = null; });
		return this.running;
	}

	/** End the reveal wait now, so a click on a pending anchor shows its arrival at once. */
	revealNow(): void { this.skipWait?.(); }

	async wake(): Promise<void> {
		if (this.paused && this.options.isViewing()) this.drain = true;
		if (this.running) await this.running;
		if (this.rows.length && this.options.isViewing()) await this.run();
	}

	private pause(): void {
		this.paused = true;
		this.options.paused?.(this.rows.map((row) => row.value));
	}

	/** Wait for `clock` (the editor's select step or the queue's own sleep); a click on the anchor ends it early. */
	private async wait(clock: Promise<unknown>): Promise<void> {
		const skipped = new Promise<void>((resolve) => { this.skipWait = resolve; });
		await Promise.race([clock, skipped]);
		this.skipWait = null;
	}

	/** Reveal, then clear: the highlight never outlives a reveal that throws. */
	private async show(value: T, linger?: () => Promise<void>): Promise<void> {
		try {
			await this.options.reveal(value);
			await linger?.();
		} finally {
			await this.options.clear(value);
		}
	}

	private async pump(): Promise<void> {
		while (this.rows.length) {
			if (!this.options.isViewing()) { this.pause(); return; }
			const entry = this.rows.shift()!;
			const prepared = entry.prepared ?? await this.options.prepare(entry.value);
			if (this.drain) {
				await this.options.reveal(prepared.value);
				continue;
			}
			const reduced = this.options.reducedMotion();
			// Chosen once per arrival: an editor that can run the select owns the timeline, else the queue holds.
			const selecting = reduced ? null : this.options.select?.(prepared.value) ?? null;
			await this.options.highlight(prepared.value, selecting !== null);
			if (reduced) {
				await this.show(prepared.value, () => (this.options.sleep ?? sleep)(REDUCED_HIGHLIGHT_FOR));
				continue;
			}
			await this.wait(selecting ?? (this.options.sleep ?? sleep)(prepared.delayMs ?? revealDelay(prepared.chars)));
			if (!this.options.isViewing()) {
				await this.options.clear(prepared.value);
				this.rows.unshift({ value: entry.value, prepared });
				this.pause();
				return;
			}
			await this.show(prepared.value);
		}
		this.paused = false;
		this.drain = false;
	}
}
