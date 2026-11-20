import { describe, expect, it, vi } from 'vitest';
import {
	BUCKET_UPPERS,
	createClientTiming,
	detectEngine,
	routeOf,
	splitEntry,
	type ClientTimingRow,
	type TimingEntry,
} from './clientTiming';

const base: TimingEntry = {
	name: 'http://x/api/threads/abc',
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
	it('falls back to an unknown route', () => {
		expect(routeOf({ ...base, serverTiming: [] })).toEqual({ method: 'OTHER', route: '/<unknown>' });
	});
});

describe('detectEngine', () => {
	it('tells the desktop webview from Chromium', () => {
		expect(detectEngine('Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15')).toBe('webkit');
		expect(detectEngine('Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari/537.36')).toBe('chromium');
		expect(detectEngine('Mozilla/5.0 Gecko/20100101 Firefox/120.0')).toBe('gecko');
	});
});

describe('createClientTiming', () => {
	function setup() {
		const sent: ClientTimingRow[][] = [];
		const timers: Array<() => void> = [];
		const timing = createClientTiming({
			send: (rows) => {
				sent.push(rows);
				return Promise.resolve();
			},
			engine: 'webkit',
			setTimer: (run) => timers.push(run),
		});
		return { sent, timers, timing };
	}

	it('aggregates per measure and sends one batch from one single-shot timer', () => {
		const { sent, timers, timing } = setup();
		for (let i = 0; i < 25; i += 1) timing.record(base, 0);
		expect(timers).toHaveLength(1);
		expect(sent).toHaveLength(0);
		timers[0]();
		expect(sent).toHaveLength(1);
		const wait = sent[0].find((row) => row.measure === 'wait')!;
		expect(wait).toMatchObject({ count: 25, sum_ms: 750, max_ms: 30, engine: 'webkit', method: 'GET' });
		expect(wait.buckets).toHaveLength(BUCKET_UPPERS.length);
		expect(wait.buckets.reduce((a, b) => a + b, 0)).toBe(25);
		expect(wait.buckets[BUCKET_UPPERS.indexOf(32)]).toBe(25);
	});

	it('counts a measure only for requests that filled it', () => {
		const { sent, timers, timing } = setup();
		timing.record(base, 0);
		timing.record({ ...base, requestStart: 0 }, 0);
		timers[0]();
		const count = (measure: string) => sent[0].find((row) => row.measure === measure)!.count;
		expect([count('total'), count('wait'), count('server')]).toEqual([2, 1, 2]);
	});

	it('sends nothing and arms nothing when no sample arrived', () => {
		const { sent, timers, timing } = setup();
		timing.flush();
		expect(sent).toHaveLength(0);
		expect(timers).toHaveLength(0);
	});

	it('drops a failed batch without retrying', async () => {
		const send = vi.fn(() => Promise.reject(new Error('offline')));
		const timers: Array<() => void> = [];
		const timing = createClientTiming({ send, engine: 'other', setTimer: (run) => timers.push(run) });
		timing.record(base, 0);
		timers[0]();
		await Promise.resolve();
		expect(send).toHaveBeenCalledTimes(1);
		expect(timers).toHaveLength(1);
	});
});
