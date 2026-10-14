import { afterEach, describe, expect, it, vi } from 'vitest';
import { token } from './auth';
import { waitForSystemRestart } from './settingsSystemRestart';

const oldHash = '0f0294a7b54e' + '0'.repeat(28);
const newHash = '6142075b1cbd' + '1'.repeat(28);
const before = {
	frontend: { full_hash: oldHash },
	backend: { full_hash: oldHash, start_iso: 'old-start', current_head: { full_hash: oldHash } },
};
const completedVersion = {
	frontend: { full_hash: newHash },
	backend: { full_hash: newHash, start_iso: 'replacement-start', current_head: { full_hash: newHash } },
};

function json(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('waitForSystemRestart', () => {
	afterEach(() => {
		vi.useRealTimers();
		vi.restoreAllMocks();
		token.set(null);
	});

	it('waits through an unreachable restarting backend and reads the terminal operation after reconnect', async () => {
		vi.useFakeTimers();
		token.set('test-token');
		let reads = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			reads++;
			if (reads <= 2) throw new Error('backend unavailable');
			if (String(input) === '/api/version') return json(completedVersion);
			return json({ status: 'failed', failure_code: 'build_failed', error: 'build summary' });
		}));

		const waiting = waitForSystemRestart('restart', before, 'operation-1');
		await vi.advanceTimersByTimeAsync(500);

		await expect(waiting).resolves.toEqual({ ok: false, message: 'build summary' });
	});

	it('reports a cancelled plain restart with its stored summary, not update wording', async () => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
			String(input) === '/api/version'
				? json(completedVersion)
				: json({
					status: 'cancelled',
					failure_code: 'drain_cancelled',
					error: 'restart control cancelled during steering',
				})
		)));

		await expect(waitForSystemRestart('restart', before, 'operation-2')).resolves.toEqual({
			ok: false,
			message: 'restart control cancelled during steering',
		});
	});

	it('keeps ordinary polling success on the existing strict predicate', async () => {
		vi.useFakeTimers();
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
			String(input) === '/api/version'
				? json(Date.now() >= 500 ? completedVersion : before)
				: json(Date.now() >= 500 ? { status: 'complete' } : { status: 'in_flight', phase: 'restarting' })
		)));

		const waiting = waitForSystemRestart('restart', before, 'operation-strict');
		await vi.advanceTimersByTimeAsync(500);

		await expect(waiting).resolves.toEqual({ ok: true });
	});

	it('preserves a missing operation result before version reconciliation', async () => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
			String(input) === '/api/version' ? json(completedVersion) : new Response(null, { status: 404 })
		)));

		await expect(waitForSystemRestart('restart', before, 'missing-operation')).resolves.toEqual({
			ok: false,
			message: 'Restart operation not found.',
		});
	});

	it('requires an unavailable observation before accepting a replacement when the initial start is missing', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
			String(input) === '/api/version'
				? json(Date.now() >= 500 ? completedVersion : null)
				: json({ status: 'complete' })
		)));

		const waiting = waitForSystemRestart('restart', null, 'operation-missing-start');
		await vi.advanceTimersByTimeAsync(500);
		await expect(waiting).resolves.toEqual({ ok: true });
	});

	it('reports a terminal version mismatch after a ten-second settle', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
			String(input) === '/api/version'
				? json({ ...completedVersion, frontend: { full_hash: oldHash } })
				: json({ status: 'complete' })
		)));

		const waiting = waitForSystemRestart('update', before, 'operation-mismatch');
		await vi.advanceTimersByTimeAsync(10_000);

		await expect(waiting).resolves.toMatchObject({ ok: false, message: expect.stringContaining('frontend') });
	});

	it('continues after a five-second hung observation and accepts a later strict match', async () => {
		vi.useFakeTimers();
		token.set('test-token');
		let reads = 0;
		let aborted = 0;
		vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
			reads++;
			if (reads <= 2) return new Promise<Response>((_resolve) => {
				init?.signal?.addEventListener('abort', () => { aborted++; });
			});
			return Promise.resolve(String(input) === '/api/version' ? json(completedVersion) : json({ status: 'complete' }));
		}));

		const waiting = waitForSystemRestart('update', before, 'operation-hung-poll');
		await vi.advanceTimersByTimeAsync(5_500);

		await expect(waiting).resolves.toEqual({ ok: true });
		expect(aborted).toBe(2);
	});

	it('keeps polling an in-flight restarting operation instead of manufacturing an unverified result', async () => {
		vi.useFakeTimers();
		token.set('test-token');
		let reads = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			reads++;
			if (String(input) === '/api/version') return json(reads > 4 ? completedVersion : before);
			return json(reads > 4 ? { status: 'complete' } : { status: 'in_flight', phase: 'restarting' });
		}));

		const waiting = waitForSystemRestart('update', before, 'operation-3');
		await vi.advanceTimersByTimeAsync(1_000);

		await expect(waiting).resolves.toEqual({ ok: true });
	});
});
