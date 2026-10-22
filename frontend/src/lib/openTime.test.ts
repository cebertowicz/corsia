import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import { token } from '$lib/stores/auth';
import { _resetOpenTimeForTests, openMilliseconds, openRangeKey, openTimes, requestOpenTime } from './openTime';

const MIN = 60_000;
const key = openRangeKey('t1', { since: 1_000, until: null });

describe('openMilliseconds', () => {
	beforeEach(() => _resetOpenTimeForTests());

	it('reads nothing before the server has answered', () => {
		expect(openMilliseconds(new Map(), key, 5 * MIN)).toBeNull();
	});

	it('counts up from the moment the answer arrived while the work is open, free of the server clock', () => {
		const readings = new Map([[key, { seconds: 120, openNow: true, receivedAt: 10 * MIN }]]);
		expect(openMilliseconds(readings, key, 10 * MIN)).toBe(2 * MIN);
		expect(openMilliseconds(readings, key, 13 * MIN)).toBe(5 * MIN);
	});

	it('holds a settled answer still', () => {
		const readings = new Map([[key, { seconds: 120, openNow: false, receivedAt: 10 * MIN }]]);
		expect(openMilliseconds(readings, key, 99 * MIN)).toBe(2 * MIN);
	});

	it('never goes back across Working, Waiting, Working, Worked', () => {
		const at = (seconds: number, openNow: boolean, receivedAt: number) => new Map([[key, { seconds, openNow, receivedAt }]]);
		expect(openMilliseconds(at(60, true, 0), key, 0)).toBe(MIN);
		// Waiting: a later answer that counts less does not lower the shown number.
		expect(openMilliseconds(at(30, true, 15_000), key, 15_000)).toBe(MIN);
		// Working again, then the run ends with a figure still below what showed.
		expect(openMilliseconds(at(90, true, 30_000), key, 45_000)).toBe(105_000);
		expect(openMilliseconds(at(100, false, 60_000), key, 90_000)).toBe(105_000);
		// Worked: the server's final figure, once it passes what showed, is what reads.
		expect(openMilliseconds(at(200, false, 90_000), key, 120_000)).toBe(200_000);
	});
});

describe('requestOpenTime', () => {
	beforeEach(() => {
		_resetOpenTimeForTests();
		token.set('tok');
	});
	afterEach(() => {
		token.set(null);
		vi.unstubAllGlobals();
	});

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

	it('leaves a thread the server left out unanswered, and a failed read unchanged', async () => {
		const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: {} }) }) as Response);
		vi.stubGlobal('fetch', fetchMock);
		requestOpenTime([{ key: 'a', threadId: 'other', since: 0, until: null }]);
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(get(openTimes).has('a')).toBe(false);

		fetchMock.mockImplementation(async () => ({ ok: false, status: 500 }) as Response);
		requestOpenTime([{ key: 'a', threadId: 'other', since: 0, until: null }]);
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(get(openTimes).has('a')).toBe(false);
	});
});
