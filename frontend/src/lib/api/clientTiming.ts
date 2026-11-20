/**
 * Where an /api request's time went, as the browser saw it.
 *
 * For each /api request the page reads its resource timing entry and splits the time into the wait
 * inside the browser (queued behind the per-host connection limit, stalled), connection setup, the
 * server's own duration (from the response's `Server-Timing` header) and the network remainder. The
 * split is aggregated in memory per day, engine, method and route template, and sent in one small
 * batch at most once a minute, so a day of use says whether waiting in the browser or server work is
 * most of a slow chat switch.
 *
 * A field the engine leaves unfilled makes that measure unknown for that request: it is left out, never
 * counted as 0. Only durations, the method and the route template leave this module; never a path,
 * a query, an id or a header. The module owns no timer loop: one single-shot timer is armed when the
 * first sample arrives and nothing runs while the page is idle.
 */

/** Same upper edges (ms) as the server's route timing histogram, so both read with one function. */
export const BUCKET_UPPERS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1_024, 2_048, 4_096, 8_192, 16_384, 32_768, 60_000];
export const MEASURES = ['total', 'wait', 'connect', 'server', 'network'] as const;
export type Measure = (typeof MEASURES)[number];
export type Engine = 'chromium' | 'webkit' | 'gecko' | 'other';

export const CLIENT_TIMING_PATH = '/api/system/perf/client-timing';
const FLUSH_MS = 60_000;
/** One batch is the most the server accepts; a window that sees more distinct keys drops the rest. */
const MAX_KEYS = 500;
const UNKNOWN_ROUTE = '/<unknown>';

export interface ClientTimingRow {
	day: string;
	engine: Engine;
	method: string;
	route: string;
	measure: Measure;
	count: number;
	sum_ms: number;
	max_ms: number;
	buckets: number[];
}

/** Durations in ms that the entry let us derive; a measure that could not be derived is absent. */
export type Split = Partial<Record<Measure, number>>;

export interface TimingEntry {
	name: string;
	initiatorType?: string;
	startTime: number;
	domainLookupStart?: number;
	connectStart?: number;
	connectEnd?: number;
	requestStart?: number;
	responseEnd?: number;
	serverTiming?: ReadonlyArray<{ name: string; duration: number; description?: string }>;
}

export function detectEngine(userAgent: string): Engine {
	if (/Chrome|Chromium|CriOS|Edg\//.test(userAgent)) return 'chromium';
	if (/Firefox|FxiOS/.test(userAgent)) return 'gecko';
	if (/AppleWebKit/.test(userAgent)) return 'webkit';
	return 'other';
}

function filled(value: number | undefined): value is number {
	return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

/** The method and route template the server named in `Server-Timing`, if it was readable. */
export function routeOf(entry: TimingEntry): { method: string; route: string } {
	const named = entry.serverTiming?.find((item) => item.name === 'route')?.description ?? '';
	const space = named.indexOf(' ');
	if (space > 0 && named.charAt(space + 1) === '/') return { method: named.slice(0, space), route: named.slice(space + 1) };
	return { method: 'OTHER', route: UNKNOWN_ROUTE };
}

/** Split one entry's time. Returns null for an entry that is not a finished /api fetch. */
export function splitEntry(entry: TimingEntry): Split | null {
	const end = entry.responseEnd;
	if (!filled(end) || end < entry.startTime) return null;
	const split: Split = { total: end - entry.startTime };
	const sent = entry.requestStart;
	if (filled(sent) && sent >= entry.startTime) {
		let setup = 0;
		const connectStart = entry.connectStart;
		const connectEnd = entry.connectEnd;
		if (filled(connectStart) && filled(connectEnd) && connectEnd >= connectStart) {
			const lookup = entry.domainLookupStart;
			setup = connectEnd - (filled(lookup) && lookup <= connectStart ? lookup : connectStart);
		}
		const wait = sent - entry.startTime - setup;
		if (wait >= -1) split.wait = Math.max(0, wait);
		if (setup > 0) split.connect = setup;
	}
	const app = entry.serverTiming?.find((item) => item.name === 'app')?.duration;
	if (typeof app === 'number' && Number.isFinite(app) && app >= 0) {
		split.server = app;
		if (filled(sent) && end >= sent) split.network = Math.max(0, end - sent - app);
	}
	return split;
}

function bucketIndex(ms: number): number {
	for (let index = 0; index < BUCKET_UPPERS.length; index += 1) if (ms <= BUCKET_UPPERS[index]) return index;
	return BUCKET_UPPERS.length - 1;
}

interface Cell {
	row: ClientTimingRow;
}

export function createClientTiming(options: {
	send: (rows: ClientTimingRow[], keepalive: boolean) => Promise<unknown>;
	engine: Engine;
	setTimer?: (run: () => void, ms: number) => unknown;
}) {
	let cells = new Map<string, Cell>();
	let armed = false;
	const setTimer = options.setTimer ?? ((run: () => void, ms: number) => setTimeout(run, ms));

	function add(day: string, method: string, route: string, measure: Measure, ms: number): void {
		const key = `${day}\u0000${method}\u0000${route}\u0000${measure}`;
		let cell = cells.get(key);
		if (!cell) {
			if (cells.size >= MAX_KEYS) return;
			cell = {
				row: { day, engine: options.engine, method, route, measure, count: 0, sum_ms: 0, max_ms: 0, buckets: BUCKET_UPPERS.map(() => 0) },
			};
			cells.set(key, cell);
		}
		const row = cell.row;
		row.count += 1;
		row.sum_ms += ms;
		row.max_ms = Math.max(row.max_ms, ms);
		row.buckets[bucketIndex(ms)] += 1;
	}

	function flush(keepalive = false): void {
		armed = false;
		if (cells.size === 0) return;
		const rows = [...cells.values()].map((cell) => cell.row);
		cells = new Map();
		// One attempt: a failed batch is dropped, since this is measurement and never worth a retry.
		options.send(rows, keepalive).catch(() => undefined);
	}

	function record(entry: TimingEntry, epochMs: number): void {
		const split = splitEntry(entry);
		if (!split) return;
		const { method, route } = routeOf(entry);
		const day = new Date(epochMs + entry.startTime).toISOString().slice(0, 10);
		for (const measure of MEASURES) {
			const ms = split[measure];
			if (ms !== undefined) add(day, method, route, measure, ms);
		}
		if (!armed) {
			armed = true;
			setTimer(() => flush(), FLUSH_MS);
		}
	}

	return { record, flush };
}

let started = false;

/** Install the page's single observer. Safe to call on every request; only the first does anything. */
export function startClientTiming(send: (rows: ClientTimingRow[], keepalive: boolean) => Promise<unknown>): void {
	if (started) return;
	started = true;
	try {
		if (typeof PerformanceObserver === 'undefined' || typeof performance === 'undefined') return;
		const timing = createClientTiming({ send, engine: detectEngine(globalThis.navigator?.userAgent ?? '') });
		const observer = new PerformanceObserver((list) => {
			for (const entry of list.getEntries() as unknown as Array<PerformanceResourceTiming>) {
				if (entry.initiatorType !== 'fetch' && entry.initiatorType !== 'xmlhttprequest') continue;
				let path: string;
				try {
					path = new URL(entry.name).pathname;
				} catch {
					continue;
				}
				if (!path.startsWith('/api/') || path === CLIENT_TIMING_PATH) continue;
				timing.record(entry, performance.timeOrigin);
			}
		});
		observer.observe({ type: 'resource' });
		if (typeof document !== 'undefined') {
			document.addEventListener('visibilitychange', () => {
				if (document.visibilityState === 'hidden') timing.flush(true);
			});
		}
	} catch {
		// Measurement must never affect the app.
	}
}

export function _resetClientTimingForTests(): void {
	started = false;
}
