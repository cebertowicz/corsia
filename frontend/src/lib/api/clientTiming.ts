/**
 * Where an /api request's time went, as the browser saw it.
 *
 * For each /api request the page reads its resource timing entry and splits the time into the wait
 * inside the browser (queued behind the per-host connection limit, stalled), connection setup, the
 * server's own duration (from the response's `Server-Timing` header) and the network remainder. The
 * split is aggregated in memory per day, method and route template, and sent in small batches
 * at most once a minute, so a day of use says whether waiting in the browser or server work is most of
 * a slow chat switch.
 *
 * Diagnostic: delete it with the server's table and endpoints once the decision it informs is made.
 *
 * A field the engine leaves unfilled makes that measure unknown for that request: left out, never 0.
 * Only durations, the method and the server's route template leave this module; never a path, query, id
 * or header. One single-shot timer is armed when the first sample (or a capture failure) arrives; an
 * idle page runs nothing. Each failure kind is counted for the page load, logged once to the console,
 * and the counts ride on the next batch, where the server logs them; no failure path throws.
 */
import { STATS_BUCKET_UPPERS } from '$lib/browserPaneStats';

/** Upper edges (ms) of the server's route timing histogram; the one client copy, tied to the server's by a test. */
export const BUCKET_UPPERS = STATS_BUCKET_UPPERS;
export const MEASURES = ['total', 'wait', 'connect', 'server', 'network'] as const;
export type Measure = (typeof MEASURES)[number];
export type Engine = 'chromium' | 'webkit' | 'gecko' | 'other';

export const CLIENT_TIMING_PATH = '/api/system/perf/client-timing';
/** The key for a request whose response named no route (an engine that hides Server-Timing). The server accepts this one non-template route. */
export const UNKNOWN_ROUTE = '/<unknown>';

/** The server's own request-timing flush interval (`_FLUSH_SECONDS`, 60 s, checked on dev and tied to it by a test). A failed POST loses at most a minute of samples, and one POST per open page per minute is small next to the /api traffic the page already makes. */
const FLUSH_MS = 60_000;
/** Rows per POST: the server's limit. The largest row measured is 250 bytes (the longest registered route, five-digit counts), so a batch is under 25 KiB. */
export const BATCH_ROWS = 100;
/** The browser refuses keepalive bodies past 64 KiB in flight across the page; 48 KiB leaves room for other keepalive sends. */
export const KEEPALIVE_BODY_BYTES = 48 * 1024;
/** What a body carries besides its rows: the engine, sent once per body, and up to MAX_ISSUE_KEYS issues, counted against the budget for each body. */
const KEEPALIVE_BODY_OVERHEAD = 512;
/** Matches the server's caps: a count past this is plainly broken; the server refuses more keys. */
const ISSUE_COUNT_CAP = 1_000;
const MAX_ISSUE_KEYS = 8;
/** Resource timestamps are rounded by the browser; a request "sent" up to a millisecond before it started is rounding, not a measurement. */
const TIMER_ROUNDING_MS = 1;

export interface ClientTimingRow {
	day: string;
	method: string;
	route: string;
	measure: Measure;
	count: number;
	sum_ms: number;
	max_ms: number;
	buckets: number[];
}

export interface ClientTimingBatch {
	engine: Engine;
	rows: ClientTimingRow[];
	/** Counts of what went wrong in this capture: `observer_install_failed`, `server_dropped`, `post_failed:<status>` or `post_failed:network`. */
	issues?: Record<string, number>;
}

export type SendBatch = (batch: ClientTimingBatch, keepalive: boolean) => Promise<Response>;

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
		if (wait >= -TIMER_ROUNDING_MS) split.wait = Math.max(0, wait);
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

/** A row as sent: sums to the microsecond, so a row stays small. */
function wire(row: ClientTimingRow): ClientTimingRow {
	return { ...row, sum_ms: Math.round(row.sum_ms * 1000) / 1000, max_ms: Math.round(row.max_ms * 1000) / 1000 };
}

export function createClientTiming(options: {
	send: SendBatch;
	engine: Engine;
	setTimer?: (run: () => void, ms: number) => unknown;
	report?: (message: string) => void;
}) {
	// Keys come only from the route templates the server names, so this map is bounded by the server's route table.
	let cells = new Map<string, ClientTimingRow>();
	let issues: Record<string, number> = {};
	const reported = new Set<string>();
	let armed = false;
	const setTimer = options.setTimer ?? ((run: () => void, ms: number) => setTimeout(run, ms));
	const report = options.report ?? ((message: string) => console.error(message));

	function arm(): void {
		if (!armed) setTimer(() => flush(false), FLUSH_MS);
		armed = true;
	}

	function addIssue(kind: string, by = 1): void {
		if (!(kind in issues) && Object.keys(issues).length >= MAX_ISSUE_KEYS) return;
		issues[kind] = Math.min((issues[kind] ?? 0) + by, ISSUE_COUNT_CAP);
	}

	/** Count a failure of this capture, and say so once per kind for the page load. */
	function noteIssue(kind: string, detail = ''): void {
		addIssue(kind);
		if (reported.has(kind)) return;
		reported.add(kind);
		try { report(`[client timing] ${kind} (${options.engine})${detail ? `: ${detail}` : ''}`); } catch {} // a failing logger cannot be reported to itself
	}

	/** One POST. Resolves true when the server took it; every failure becomes a count that rides on the next batch. */
	async function post(rows: ClientTimingRow[], keepalive: boolean): Promise<boolean> {
		const taken = issues;
		issues = {};
		const batch: ClientTimingBatch = { engine: options.engine, rows: rows.map(wire) };
		if (Object.keys(taken).length > 0) batch.issues = taken;
		const restore = () => Object.entries(taken).forEach(([kind, count]) => addIssue(kind, count));
		try {
			const response = await options.send(batch, keepalive);
			if (!response.ok) {
				restore();
				noteIssue(`post_failed:${response.status}`);
				return false;
			}
			const body = (await response.json().catch(() => null)) as { dropped?: unknown } | null;
			if (typeof body?.dropped === 'number' && body.dropped > 0) noteIssue('server_dropped', `${body.dropped} of ${rows.length} rows rejected`);
			return true;
		} catch (error) {
			restore();
			noteIssue('post_failed:network', error instanceof Error ? error.message : '');
			return false;
		}
	}

	/** The bodies a page-hide flush may send: batches of up to BATCH_ROWS, all together inside the keepalive budget. */
	function takeForKeepalive(): ClientTimingRow[][] {
		const bodies: ClientTimingRow[][] = [];
		let bytes = 0;
		for (const [key, row] of cells) {
			const size = JSON.stringify(wire(row)).length + 1;
			let body = bodies[bodies.length - 1];
			const overhead = !body || body.length >= BATCH_ROWS ? KEEPALIVE_BODY_OVERHEAD : 0;
			if (bytes + overhead + size > KEEPALIVE_BODY_BYTES) break;
			if (overhead) bodies.push((body = []));
			body.push(row);
			bytes += overhead + size;
			cells.delete(key);
		}
		return bodies;
	}

	/** Send what is pending: on page hide every body that fits the keepalive budget at once (a background tab sends the rest on the armed timer); otherwise batches in turn, stopping at the first failure. */
	function flush(hidden: boolean): void {
		if (!hidden) armed = false;
		if (cells.size === 0 && !issues.observer_install_failed) return;
		if (hidden) {
			const bodies = takeForKeepalive();
			for (const rows of bodies.length ? bodies : [[]]) void post(rows, true); // [[]]: issues only
			return;
		}
		const all = [...cells.values()];
		cells = new Map();
		const batches: ClientTimingRow[][] = [];
		for (let at = 0; at < all.length; at += BATCH_ROWS) batches.push(all.slice(at, at + BATCH_ROWS));
		if (batches.length === 0) batches.push([]);
		void (async () => {
			for (const rows of batches) if (!(await post(rows, false))) return;
		})();
	}

	function add(day: string, method: string, route: string, measure: Measure, ms: number): void {
		const key = `${day}\u0000${method}\u0000${route}\u0000${measure}`;
		let row = cells.get(key);
		if (!row) {
			row = { day, method, route, measure, count: 0, sum_ms: 0, max_ms: 0, buckets: BUCKET_UPPERS.map(() => 0) };
			cells.set(key, row);
		}
		row.count += 1;
		row.sum_ms += ms;
		row.max_ms = Math.max(row.max_ms, ms);
		row.buckets[bucketIndex(ms)] += 1;
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
		arm();
	}

	/** The observer could not be installed: say so once, and let the next flush carry one issues-only batch. */
	function installFailed(error: unknown): void {
		noteIssue('observer_install_failed', error instanceof Error ? error.message : '');
		arm();
	}

	return { record, flush, installFailed };
}

/** Whether a resource entry is one of the page's own /api fetches. The flush's own POST is not: measuring it would arm another flush forever. */
export function isApiFetch(entry: TimingEntry): boolean {
	if (entry.initiatorType !== 'fetch' && entry.initiatorType !== 'xmlhttprequest') return false;
	try {
		const path = new URL(entry.name).pathname;
		return path.startsWith('/api/') && path !== CLIENT_TIMING_PATH;
	} catch {
		return false;
	}
}

let started = false;
/** Install the page's single observer. Safe to call on every request; only the first does anything. */
export function startClientTiming(send: SendBatch): void {
	if (started) return;
	started = true;
	const timing = createClientTiming({ send, engine: detectEngine(globalThis.navigator?.userAgent ?? '') });
	try {
		if (typeof PerformanceObserver === 'undefined' || typeof performance === 'undefined') {
			throw new Error('PerformanceObserver is not available');
		}
		const observer = new PerformanceObserver((list) => {
			for (const entry of list.getEntries() as unknown as Array<PerformanceResourceTiming>) {
				if (isApiFetch(entry)) timing.record(entry, performance.timeOrigin);
			}
		});
		observer.observe({ type: 'resource' });
		if (typeof document !== 'undefined') {
			document.addEventListener('visibilitychange', () => {
				if (document.visibilityState === 'hidden') timing.flush(true);
			});
		}
	} catch (error) {
		timing.installFailed(error);
	}
}

export function _resetClientTimingForTests(): void {
	started = false;
}
