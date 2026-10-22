import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import { token } from '$lib/stores/auth';
import { _resetOpenTimeForTests, openMilliseconds, openRangeKey, openTimes, requestOpenTime } from './openTime';

const MIN = 60_000;
const key = openRangeKey('t1', { since: 1_000, until: null });

function answering(seconds: number, openNow: boolean): Response {
	return { ok: true, status: 200, json: async () => ({ items: { [key]: { seconds, open_now: openNow, as_of: 'x' } } }) } as Response;
}

describe('openMilliseconds', () => {
	beforeEach(() => _resetOpenTimeForTests());

	it('reads nothing before the server has answered', () => {
		expect(openMilliseconds(new Map(), key, 5 * MIN)).toBeNull();
	});

	it('counts up from the moment the answer arrived while the work is open, free of the server clock', () => {
		const readings = new Map([[key, { seconds: 120, openNow: true, receivedAt: 10 * MIN, floor: 0 }]]);
		expect(openMilliseconds(readings, key, 10 * MIN)).toBe(2 * MIN);
		expect(openMilliseconds(readings, key, 13 * MIN)).toBe(5 * MIN);
	});

	it('holds a settled answer still', () => {
		const readings = new Map([[key, { seconds: 120, openNow: false, receivedAt: 10 * MIN, floor: 0 }]]);
		expect(openMilliseconds(readings, key, 99 * MIN)).toBe(2 * MIN);
	});
});

describe('requestOpenTime', () => {
	beforeEach(() => {
		_resetOpenTimeForTests();
		token.set('tok');
	});
	afterEach(() => {
		token.set(null);
		vi.useRealTimers();
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	async function answer(fetchMock: ReturnType<typeof vi.fn>, response: Response, at: number): Promise<void> {
		vi.spyOn(Date, 'now').mockReturnValue(at);
		fetchMock.mockResolvedValueOnce(response);
		const calls = fetchMock.mock.calls.length;
		requestOpenTime([{ key, threadId: 't1', since: 1_000, until: null }]);
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(calls + 1));
		await vi.waitFor(() => expect(get(openTimes).get(key)?.receivedAt).toBe(at));
	}

	it('shares one request between same-tick asks and stores each answer by key', async () => {
		const fetchMock = vi.fn(async (_url: unknown, init?: RequestInit) => {
			const { items } = JSON.parse(String(init?.body)) as { items: { id: string }[] };
			return {
				ok: true, status: 200,
				json: async () => ({ items: Object.fromEntries(items.map(({ id }) => [id, { seconds: 7, open_now: true, as_of: 'x' }])) }),
			} as Response;
		});
		vi.stubGlobal('fetch', fetchMock);
		requestOpenTime([{ key: 'a', threadId: 't1', since: 0, until: null }]);
		requestOpenTime([{ key: 'b', threadId: 't2', since: 0, until: 5 }]);
		await vi.waitFor(() => expect(get(openTimes).size).toBe(2));

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(get(openTimes).get('b')?.seconds).toBe(7);
	});

	it('never goes back while open across Working, Waiting, Working; Worked reads the server figure', async () => {
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
		const reads = (at: number) => openMilliseconds(get(openTimes), key, at);

		await answer(fetchMock, answering(60, true), 0);
		expect(reads(0)).toBe(MIN);
		// Waiting: a later open answer that counts less does not lower what the count had reached.
		await answer(fetchMock, answering(30, true), 15_000);
		expect(reads(15_000)).toBe(75_000);
		// Working again: the count resumes once it passes that floor.
		await answer(fetchMock, answering(90, true), 30_000);
		expect(reads(30_000)).toBe(90_000);
		expect(reads(45_000)).toBe(105_000);
		// Worked: the settled figure is the server's, even below where the open count had reached.
		await answer(fetchMock, answering(100, false), 60_000);
		expect(reads(90_000)).toBe(100_000);
		// A run that opens again starts from the server's figure, not the old count.
		await answer(fetchMock, answering(110, true), 120_000);
		expect(reads(120_000)).toBe(110_000);
	});

	it('leaves a thread the server left out unanswered', async () => {
		const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: {} }) }) as Response);
		vi.stubGlobal('fetch', fetchMock);
		requestOpenTime([{ key: 'a', threadId: 'other', since: 0, until: null }]);
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(get(openTimes).has('a')).toBe(false);
	});

	it('retries a failed read, then logs once the retries run out', async () => {
		vi.useFakeTimers();
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const fetchMock = vi.fn(async () => ({ ok: false, status: 500 }) as Response);
		vi.stubGlobal('fetch', fetchMock);
		requestOpenTime([{ key, threadId: 't1', since: 1_000, until: null }]);
		await vi.runAllTimersAsync();

		expect(fetchMock).toHaveBeenCalledTimes(4);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(get(openTimes).has(key)).toBe(false);
	});

	it('shows the answer once a retry succeeds', async () => {
		vi.useFakeTimers();
		const fetchMock = vi.fn()
			.mockRejectedValueOnce(new Error('offline'))
			.mockResolvedValueOnce(answering(42, false));
		vi.stubGlobal('fetch', fetchMock);
		requestOpenTime([{ key, threadId: 't1', since: 1_000, until: null }]);
		await vi.runAllTimersAsync();

		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(get(openTimes).get(key)?.seconds).toBe(42);
	});

	it('holds asks made while signed out and sends them once a token arrives', async () => {
		token.set(null);
		const fetchMock = vi.fn(async () => answering(9, false));
		vi.stubGlobal('fetch', fetchMock);
		requestOpenTime([{ key, threadId: 't1', since: 1_000, until: null }]);
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(fetchMock).not.toHaveBeenCalled();

		token.set('tok');
		await vi.waitFor(() => expect(get(openTimes).get(key)?.seconds).toBe(9));
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});
});
