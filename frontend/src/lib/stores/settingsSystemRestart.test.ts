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

		const waiting = waitForSystemRestart('update', before, 'operation-1');
		await vi.advanceTimersByTimeAsync(500);

		await expect(waiting).resolves.toEqual({
			ok: false,
			message: 'Could not build the update. CORSIA is still running.',
		});
	});

	it('shows the durable disk-space numbers in rounded GB', async () => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
			String(input) === '/api/version'
				? json(completedVersion)
				: json({
					status: 'failed',
					failure_code: 'disk_space',
					failure_detail: JSON.stringify({ needed_bytes: 12_600_000_000, free_bytes: 8_400_000_000 }),
				})
		)));

		await expect(waitForSystemRestart('update', before, 'operation-2')).resolves.toEqual({
			ok: false,
			message: 'Needs 13 GB free; this Mac has 8 GB.',
		});
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
