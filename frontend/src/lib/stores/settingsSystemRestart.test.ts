import { afterEach, describe, expect, it, vi } from 'vitest';
import { token } from './auth';
import { waitForSystemRestart } from './settingsSystemRestart';

const oldHash = '0f0294a7b54e' + '0'.repeat(28);
const newHash = '6142075b1cbd' + '1'.repeat(28);

const before = {
	frontend: { full_hash: oldHash },
	backend: {
		full_hash: oldHash,
		start_iso: 'old-start',
		current_head: { full_hash: oldHash },
	},
};

const completedVersion = {
	frontend: { full_hash: newHash },
	backend: {
		full_hash: newHash,
		start_iso: 'replacement-start',
		current_head: { full_hash: newHash },
	},
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

	it('reconciles a completion observed only after a delayed timer resumes past the polling deadline', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		token.set('test-token');
		let versionReads = 0;
		let statusReads = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/version') {
				versionReads++;
				return json(Date.now() >= 155_000 ? completedVersion : before);
			}
			if (url.includes('/api/system/drain')) {
				statusReads++;
				return json({ status: Date.now() >= 155_000 ? 'complete' : 'in_flight', phase: 'restarting' });
			}
			throw new Error(`unexpected request: ${url}`);
		}));

		const waiting = waitForSystemRestart('update', before, 'operation-1', undefined, 240_000);
		await vi.advanceTimersByTimeAsync(0);
		vi.setSystemTime(240_001);
		await vi.advanceTimersByTimeAsync(500);

		await expect(waiting).resolves.toEqual({ ok: true });
		expect(versionReads).toBe(2);
		expect(statusReads).toBe(2);
	});

	it('keeps ordinary polling success on the existing strict predicate', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input) === '/api/version') return json(Date.now() >= 500 ? completedVersion : before);
			return json({ status: Date.now() >= 500 ? 'complete' : 'in_flight', phase: 'restarting' });
		}));

		const waiting = waitForSystemRestart('restart', before, 'operation-2', undefined, 1_000);
		await vi.advanceTimersByTimeAsync(500);

		await expect(waiting).resolves.toEqual({ ok: true });
	});

	it.each([
		['failed', 'backend failed'],
		['cancelled', 'operator cancelled'],
		['superseded', 'newer operation won'],
	] as const)('preserves a %s operation reason before version reconciliation', async (status, error) => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input) === '/api/version') return json(completedVersion);
			return json({ status, error });
		}));

		await expect(waitForSystemRestart('update', before, 'operation-3')).resolves.toEqual({ ok: false, message: error });
	});

	it('preserves a missing operation result before version reconciliation', async () => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input) === '/api/version') return json(completedVersion);
			return new Response(null, { status: 404 });
		}));

		await expect(waitForSystemRestart('restart', before, 'missing-operation')).resolves.toEqual({
			ok: false,
			message: 'Restart operation not found.',
		});
	});

	it.each([
		['null version', null, { status: 'complete' }],
		['null status', completedVersion, null],
		['persistent revision mismatch', { ...completedVersion, frontend: { full_hash: oldHash } }, { status: 'complete' }],
		['complete operation without changed backend start', { ...completedVersion, backend: { ...completedVersion.backend, start_iso: 'old-start' } }, { status: 'complete' }],
	] as const)('reports inability when the final %s cannot satisfy strict completion', async (_name, version, status) => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input) === '/api/version') return json(version);
			return json(status);
		}));

		await expect(waitForSystemRestart('update', before, 'operation-4', undefined, 0)).resolves.toEqual({
			ok: false,
			message: "Couldn't verify update completion. Inspect the restart log before retrying.",
		});
	});

	it.each(['update', 'restart'] as const)(
		'reports inability for %s after a delayed timer when an earlier complete observation cannot verify replacement',
		async (mode) => {
			vi.useFakeTimers();
			vi.setSystemTime(0);
			token.set('test-token');
			vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
				if (String(input) === '/api/version') {
					return json(Date.now() === 0 ? before : { ...completedVersion, frontend: { full_hash: oldHash } });
				}
				return json({ status: 'complete' });
			}));

			const waiting = waitForSystemRestart(mode, before, 'operation-prior-complete', undefined, 240_000);
			await vi.advanceTimersByTimeAsync(0);
			vi.setSystemTime(240_001);
			await vi.advanceTimersByTimeAsync(500);

			await expect(waiting).resolves.toEqual({
				ok: false,
				message: `Couldn't verify ${mode} completion. Inspect the restart log before retrying.`,
			});
		},
	);

	it.each(['failed', 'cancelled', 'superseded'] as const)(
		'preserves an explicit final %s reason after an earlier complete observation',
		async (status) => {
			vi.useFakeTimers();
			vi.setSystemTime(0);
			token.set('test-token');
			vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
				if (String(input) === '/api/version') return json(Date.now() === 0 ? before : completedVersion);
				return json(Date.now() === 0 ? { status: 'complete' } : { status, error: 'Explicit final reason' });
			}));

			const waiting = waitForSystemRestart('update', before, 'operation-final-failure', undefined, 240_000);
			await vi.advanceTimersByTimeAsync(0);
			vi.setSystemTime(240_001);
			await vi.advanceTimersByTimeAsync(500);

			await expect(waiting).resolves.toEqual({ ok: false, message: 'Explicit final reason' });
		},
	);

	it('requires an unavailable observation before accepting a replacement when the initial start is missing', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input) === '/api/version') return json(Date.now() >= 500 ? completedVersion : null);
			return json({ status: 'complete' });
		}));

		const waiting = waitForSystemRestart('restart', null, 'operation-5', undefined, 500);
		await vi.advanceTimersByTimeAsync(500);

		await expect(waiting).resolves.toEqual({ ok: true });
	});

	it('settles after the shared final budget and ignores late successful endpoint responses', async () => {
		vi.useFakeTimers();
		token.set('test-token');
		let aborted = 0;
		let finalSignals = 0;
		vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve) => {
			if (init?.signal) {
				finalSignals++;
				init.signal.addEventListener('abort', () => {
					aborted++;
					setTimeout(() => resolve(json(completedVersion)), 0);
				});
			}
		})));

		const waiting = waitForSystemRestart('restart', before, 'operation-6', undefined, 0);
		await vi.advanceTimersByTimeAsync(5_000);

		await expect(waiting).resolves.toEqual({
			ok: false,
			message: "Couldn't verify restart completion. Inspect the restart log before retrying.",
		});
		expect(finalSignals).toBe(2);
		expect(aborted).toBe(2);
		await vi.runAllTimersAsync();
	});

	it('abandons a hung regular observation at its deadline before taking one fresh final pair', async () => {
		vi.useFakeTimers();
		token.set('test-token');
		let reads = 0;
		let aborted = 0;
		vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
			reads++;
			if (reads > 2) return Promise.resolve(String(input) === '/api/version' ? json(completedVersion) : json({ status: 'complete' }));
			return new Promise<Response>((resolve) => {
				init?.signal?.addEventListener('abort', () => {
					aborted++;
					resolve(String(input) === '/api/version' ? json(before) : json({ status: 'in_flight' }));
				});
			});
		}));

		const waiting = waitForSystemRestart('update', before, 'hung-regular', undefined, 1_000);
		await vi.advanceTimersByTimeAsync(1_000);

		await expect(waiting).resolves.toEqual({ ok: true });
		expect(reads).toBe(4);
		expect(aborted).toBe(2);
	});

	it('does not let a late regular success bypass a fresh final mismatch', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		token.set('test-token');
		const late: Array<(response: Response) => void> = [];
		let versionReads = 0;
		let statusReads = 0;
		vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
			if (String(input) === '/api/version') {
				versionReads++;
				if (versionReads > 1) return Promise.resolve(json({ ...completedVersion, frontend: { full_hash: oldHash } }));
			} else {
				statusReads++;
				if (statusReads > 1) return Promise.resolve(json({ status: 'complete' }));
			}
			return new Promise<Response>((resolve) => late.push(resolve));
		}));

		const waiting = waitForSystemRestart('update', before, 'late-regular', undefined, 1_000);
		await vi.advanceTimersByTimeAsync(0);
		vi.setSystemTime(1_001);
		late[0](json(completedVersion));
		late[1](json({ status: 'complete' }));

		await expect(waiting).resolves.toEqual({
			ok: false,
			message: "Couldn't verify update completion. Inspect the restart log before retrying.",
		});
	});

	it.each([
		['ordinary', 1_000],
		['final', 0],
	] as const)('rejects the documented unknown revision sentinel in an %s observation', async (_name, timeoutMs) => {
		token.set('test-token');
		const unknownVersion = {
			frontend: { full_hash: 'unknown' },
			backend: {
				full_hash: 'unknown',
				start_iso: 'replacement-start',
				current_head: { full_hash: 'unknown' },
			},
		};
		vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => (
			Promise.resolve(String(input) === '/api/version' ? json(unknownVersion) : json({ status: 'complete' }))
		)));

		await expect(waitForSystemRestart('restart', before, 'unknown-revision', undefined, timeoutMs)).resolves.toEqual({
			ok: false,
			message: "Couldn't verify restart completion. Inspect the restart log before retrying.",
		});
	});
});
