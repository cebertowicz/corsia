import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { STATS_BUCKET_UPPERS } from '$lib/browserPaneStats';
import {
	BATCH_ROWS,
	BUCKET_UPPERS,
	CLIENT_TIMING_PATH,
	KEEPALIVE_BODY_BYTES,
	UNKNOWN_ROUTE,
	_resetClientTimingForTests,
	createClientTiming,
	detectEngine,
	isApiFetch,
	routeOf,
	splitEntry,
	startClientTiming,
	type ClientTimingBatch,
	type TimingEntry,
} from './clientTiming';

const base: TimingEntry = {
	name: 'http://x/api/threads/abc',
	initiatorType: 'fetch',
	startTime: 100,
	domainLookupStart: 100,
	connectStart: 100,
	connectEnd: 100,
	requestStart: 130,
	responseEnd: 200,
	serverTiming: [
		{ name: 'route', duration: 0, description: 'GET /api/threads/{thread_id}' },
		{ name: 'app', duration: 40 },
	],
};

const reply = (status: number, body: unknown = {}) => new Response(JSON.stringify(body), { status });

describe('splitEntry', () => {
	it('splits browser wait, server time and network remainder', () => {
		expect(splitEntry(base)).toEqual({ total: 100, wait: 30, server: 40, network: 30 });
	});

	it('takes connection setup out of the wait and reports it apart', () => {
		const split = splitEntry({ ...base, domainLookupStart: 110, connectStart: 112, connectEnd: 120 });
		expect(split).toMatchObject({ wait: 20, connect: 10 });
	});

	it('leaves wait unknown when requestStart is not filled, never 0', () => {
		const split = splitEntry({ ...base, requestStart: 0 });
		expect(split).toEqual({ total: 100, server: 40 });
		expect('wait' in split!).toBe(false);
	});

	it('leaves server and network unknown without a Server-Timing app entry', () => {
		expect(splitEntry({ ...base, serverTiming: [] })).toEqual({ total: 100, wait: 30 });
		expect(splitEntry({ ...base, serverTiming: undefined })).toEqual({ total: 100, wait: 30 });
	});

	it('skips an entry that has not finished', () => {
		expect(splitEntry({ ...base, responseEnd: 0 })).toBeNull();
	});
});

describe('routeOf', () => {
	it('reads method and template from the route entry', () => {
		expect(routeOf(base)).toEqual({ method: 'GET', route: '/api/threads/{thread_id}' });
	});
	it('falls back to the unknown route', () => {
		expect(routeOf({ ...base, serverTiming: [] })).toEqual({ method: 'OTHER', route: UNKNOWN_ROUTE });
	});
});

describe('detectEngine', () => {
	it('tells the desktop webview from Chromium', () => {
		expect(detectEngine('Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15')).toBe('webkit');
		expect(detectEngine('Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari/537.36')).toBe('chromium');
		expect(detectEngine('Mozilla/5.0 Gecko/20100101 Firefox/120.0')).toBe('gecko');
	});
});

describe('the histogram edges', () => {
	it('are the one list the pane stats use, which a test ties to the server', () => {
		expect(BUCKET_UPPERS).toBe(STATS_BUCKET_UPPERS);
	});
});

describe('isApiFetch', () => {
	it('keeps the page own /api fetches and xhr, and nothing else', () => {
		expect(isApiFetch(base)).toBe(true);
		expect(isApiFetch({ ...base, initiatorType: 'xmlhttprequest' })).toBe(true);
		expect(isApiFetch({ ...base, name: `http://x${CLIENT_TIMING_PATH}` })).toBe(false);
		expect(isApiFetch({ ...base, name: 'http://x/assets/app.js', initiatorType: 'script' })).toBe(false);
		expect(isApiFetch({ ...base, name: 'http://x/health' })).toBe(false);
		expect(isApiFetch({ ...base, initiatorType: 'img' })).toBe(false);
		expect(isApiFetch({ ...base, name: 'not a url' })).toBe(false);
	});
});

function setup(send: (batch: ClientTimingBatch, keepalive: boolean) => Promise<Response> = () => Promise.resolve(reply(202, { kept: 99999 }))) {
	const sent: Array<{ batch: ClientTimingBatch; keepalive: boolean }> = [];
	const timers: Array<() => void> = [];
	const reports: string[] = [];
	const timing = createClientTiming({
		send: (batch, keepalive) => {
			sent.push({ batch, keepalive });
			return send(batch, keepalive);
		},
		engine: 'webkit',
		setTimer: (run) => timers.push(run),
		report: (message) => reports.push(message),
	});
	const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
	return { sent, timers, reports, timing, settle };
}

/** A route template per number, so each one is its own key. */
const routed = (n: number): TimingEntry => ({
	...base,
	serverTiming: [{ name: 'route', duration: 0, description: `GET /api/things/${n}/parts/{part_id}` }, { name: 'app', duration: 40 }],
});

describe('createClientTiming', () => {
	it('aggregates per measure and sends one batch from one single-shot timer', async () => {
		const { sent, timers, timing, settle } = setup();
		for (let i = 0; i < 25; i += 1) timing.record(base, 0);
		expect(timers).toHaveLength(1);
		expect(sent).toHaveLength(0);
		timers[0]();
		await settle();
		expect(sent).toHaveLength(1);
		expect(sent[0].keepalive).toBe(false);
		expect(sent[0].batch.engine).toBe('webkit');
		const wait = sent[0].batch.rows.find((row) => row.measure === 'wait')!;
		expect(wait).toMatchObject({ count: 25, sum_ms: 750, max_ms: 30, engine: 'webkit', method: 'GET' });
		expect(wait.buckets).toHaveLength(BUCKET_UPPERS.length);
		expect(wait.buckets.reduce((a, b) => a + b, 0)).toBe(25);
		expect(wait.buckets[BUCKET_UPPERS.indexOf(32)]).toBe(25);
		expect(sent[0].batch.issues).toBeUndefined();
	});

	it('counts a measure only for requests that filled it', async () => {
		const { sent, timers, timing, settle } = setup();
		timing.record(base, 0);
		timing.record({ ...base, requestStart: 0 }, 0);
		timers[0]();
		await settle();
		const count = (measure: string) => sent[0].batch.rows.find((row) => row.measure === measure)!.count;
		expect([count('total'), count('wait'), count('server')]).toEqual([2, 1, 2]);
	});

	it('sends nothing and arms nothing when no sample arrived', () => {
		const { sent, timers, timing } = setup();
		timing.flush(false);
		timing.flush(true);
		expect(sent).toHaveLength(0);
		expect(timers).toHaveLength(0);
	});

	it('splits a large window into batches of at most the server limit, sent one after another', async () => {
		const { sent, timers, timing, settle } = setup();
		for (let n = 0; n < 45; n += 1) timing.record(routed(n), 0); // 45 routes x 4 measures = 180 rows
		timers[0]();
		await settle();
		await settle();
		expect(sent.map((s) => s.batch.rows.length)).toEqual([100, 80]);
		expect(sent.every((s) => !s.keepalive)).toBe(true);
	});

	it('stops sending the remaining batches after a failure', async () => {
		const { sent, timers, timing, settle } = setup(() => Promise.resolve(reply(503)));
		for (let n = 0; n < 45; n += 1) timing.record(routed(n), 0);
		timers[0]();
		await settle();
		await settle();
		expect(sent).toHaveLength(1);
	});

	describe('page hide', () => {
		function worstRow(): TimingEntry {
			// The longest registered template is 77 characters; sums and counts are large.
			const route = `/api/settings/network-servers/{server_id}/models/{model_id:path}/runtime-info`;
			return { ...base, startTime: 123456.789, responseEnd: 123456.789 + 12345.678901234, requestStart: 123456.789 + 1234.5678901234,
				serverTiming: [{ name: 'route', duration: 0, description: `DELETE ${route}` }, { name: 'app', duration: 9876.54321 }] };
		}

		it('keeps one row of the worst shape near 270 bytes, so a batch fits the keepalive limit', async () => {
			const { sent, timing, settle } = setup();
			for (let i = 0; i < 12_345; i += 1) timing.record(worstRow(), 0);
			timing.flush(true);
			await settle();
			const rowBytes = Math.max(...sent[0].batch.rows.map((row) => JSON.stringify(row).length));
			expect(rowBytes).toBeLessThanOrEqual(300);
			expect(BATCH_ROWS * 300).toBeLessThan(KEEPALIVE_BODY_BYTES);
		});

		it('sends one keepalive body inside the limit and leaves the rest for the timer', async () => {
			const { sent, timers, timing, settle } = setup();
			for (let n = 0; n < 45; n += 1) timing.record(routed(n), 0); // 180 rows
			timing.flush(true);
			await settle();
			expect(sent).toHaveLength(1);
			expect(sent[0].keepalive).toBe(true);
			expect(sent[0].batch.rows.length).toBe(BATCH_ROWS);
			expect(JSON.stringify(sent[0].batch).length).toBeLessThan(KEEPALIVE_BODY_BYTES);
			// The page may only be a background tab: the timer armed by the first sample still sends the other 80.
			expect(timers).toHaveLength(1);
			timers[0]();
			await settle();
			expect(sent.map((s) => s.batch.rows.length)).toEqual([BATCH_ROWS, 80]);
		});

		it('never sends a body over the keepalive budget even with fewer rows than a batch', async () => {
			const { sent, timing, settle } = setup();
			const long = (n: number): TimingEntry => ({ ...base, serverTiming: [{ name: 'route', duration: 0, description: `GET /api/${'x'.repeat(2000)}${n}` }, { name: 'app', duration: 1 }] });
			for (let n = 0; n < 60; n += 1) timing.record(long(n), 0);
			timing.flush(true);
			await settle();
			expect(JSON.stringify(sent[0].batch).length).toBeLessThanOrEqual(KEEPALIVE_BODY_BYTES);
			expect(sent[0].batch.rows.length).toBeGreaterThan(0);
			expect(sent[0].batch.rows.length).toBeLessThan(BATCH_ROWS);
		});
	});

	describe('its own failures', () => {
		it('counts a rejected POST, logs it once, and sends the count on the next batch', async () => {
			let calls = 0;
			const { sent, timers, reports, timing, settle } = setup(() => (calls++ < 2 ? Promise.reject(new Error('offline')) : Promise.resolve(reply(202, { kept: 99 }))));
			timing.record(base, 0);
			timers[0]();
			await settle();
			timing.record(base, 0);
			timers[1]();
			await settle();
			timing.record(base, 0);
			timers[2]();
			await settle();
			expect(reports).toHaveLength(1);
			expect(reports[0]).toContain('post_failed:network');
			expect(reports[0]).toContain('webkit');
			expect(sent[0].batch.issues).toBeUndefined();
			expect(sent[1].batch.issues).toEqual({ 'post_failed:network': 1 });
			expect(sent[2].batch.issues).toEqual({ 'post_failed:network': 2 });
		});

		it('counts a non-2xx POST by status and keeps the issues it had not delivered', async () => {
			let status = 503;
			const { sent, timers, timing, settle } = setup(() => Promise.resolve(status === 503 ? reply(503) : reply(202, { kept: 99 })));
			timing.record(base, 0);
			timers[0]();
			await settle();
			status = 202;
			timing.record(base, 0);
			timers[1]();
			await settle();
			expect(sent[1].batch.issues).toEqual({ 'post_failed:503': 1 });
		});

		it('counts the rows the server did not keep', async () => {
			let kept = 1;
			const { sent, timers, reports, timing, settle } = setup(() => Promise.resolve(reply(202, { kept })));
			timing.record(base, 0); // 4 rows (total, wait, server, network)
			timers[0]();
			await settle();
			kept = 99;
			timing.record(base, 0);
			timers[1]();
			await settle();
			expect(sent[1].batch.issues).toEqual({ kept_lt_sent: 1 });
			expect(reports.some((message) => message.includes('kept_lt_sent'))).toBe(true);
		});

		it('delivers issues once they were taken by a successful POST', async () => {
			let first = true;
			const { sent, timers, timing, settle } = setup(() => {
				const fail = first;
				first = false;
				return fail ? Promise.reject(new Error('x')) : Promise.resolve(reply(202, { kept: 99 }));
			});
			timing.record(base, 0);
			timers[0]();
			await settle();
			timing.record(base, 0);
			timers[1]();
			await settle();
			timing.record(base, 0);
			timers[2]();
			await settle();
			expect(sent[2].batch.issues).toBeUndefined();
		});

		it('sends one issues-only batch when the observer could not be installed, and never retries it', async () => {
			const { sent, timers, reports, timing, settle } = setup(() => Promise.reject(new Error('offline')));
			timing.installFailed(new Error('no observer'));
			timing.installFailed(new Error('no observer'));
			expect(timers).toHaveLength(1);
			expect(reports).toHaveLength(1);
			expect(reports[0]).toContain('observer_install_failed');
			timers[0]();
			await settle();
			expect(sent).toHaveLength(1);
			expect(sent[0].batch).toEqual({ engine: 'webkit', rows: [], issues: { observer_install_failed: 2 } }); // counted twice, logged once
			expect(timers).toHaveLength(1); // the failed send armed nothing: no retry loop
		});

		it('caps how many distinct issue keys one batch carries', async () => {
			let status = 500;
			const { sent, timers, timing, settle } = setup(() => Promise.resolve(reply(status++)));
			for (let i = 0; i < 12; i += 1) {
				timing.record(base, 0);
				timers[i]();
				await settle();
			}
			expect(Object.keys(sent[11].batch.issues ?? {}).length).toBeLessThanOrEqual(8);
		});
	});
});

describe('startClientTiming', () => {
	type Callback = (list: { getEntries: () => unknown[] }) => void;
	let callback: Callback;
	let observed: unknown;
	let installError: Error | null;

	beforeEach(() => {
		vi.useFakeTimers();
		_resetClientTimingForTests();
		installError = null;
		vi.stubGlobal(
			'PerformanceObserver',
			class {
				constructor(cb: Callback) {
					callback = cb;
				}
				observe(options: unknown) {
					if (installError) throw installError;
					observed = options;
				}
			},
		);
	});

	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	const feed = (...entries: TimingEntry[]) => callback({ getEntries: () => entries });

	it('records only the page own /api fetches: its flush, other files and non-fetch entries arm nothing', async () => {
		const send = vi.fn(() => Promise.resolve(reply(202, { kept: 99 })));
		startClientTiming(send);
		expect(observed).toEqual({ type: 'resource' });

		feed(
			{ ...base, name: `http://x${CLIENT_TIMING_PATH}` },
			{ ...base, name: 'http://x/assets/app.js', initiatorType: 'script' },
			{ ...base, name: 'http://x/health' },
			{ ...base, initiatorType: 'img' },
		);
		await vi.advanceTimersByTimeAsync(5 * 60_000);
		expect(send).not.toHaveBeenCalled(); // an idle page posts nothing

		feed(base);
		await vi.advanceTimersByTimeAsync(60_000);
		expect(send).toHaveBeenCalledTimes(1);
		const batch = send.mock.calls[0][0] as ClientTimingBatch;
		expect(new Set(batch.rows.map((row) => row.route))).toEqual(new Set(['/api/threads/{thread_id}']));

		feed({ ...base, name: `http://x${CLIENT_TIMING_PATH}` }); // the flush's own entry arrives
		await vi.advanceTimersByTimeAsync(5 * 60_000);
		expect(send).toHaveBeenCalledTimes(1); // and does not arm another flush
	});

	it('starts once however many requests call it', () => {
		const send = vi.fn(() => Promise.resolve(reply(202)));
		startClientTiming(send);
		const first = callback;
		startClientTiming(send);
		expect(callback).toBe(first);
	});

	it('reports an install failure with the engine and sends one issues-only batch', async () => {
		installError = new Error('resource timing is blocked');
		const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
		const send = vi.fn(() => Promise.resolve(reply(202)));
		startClientTiming(send);
		expect(error).toHaveBeenCalledTimes(1);
		expect(String(error.mock.calls[0][0])).toContain('observer_install_failed');
		await vi.advanceTimersByTimeAsync(60_000);
		expect(send).toHaveBeenCalledTimes(1);
		expect((send.mock.calls[0] as unknown[])[0]).toMatchObject({ rows: [], issues: { observer_install_failed: 1 } });
	});
});
