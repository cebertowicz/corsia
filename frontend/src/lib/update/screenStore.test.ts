import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { clearStartupProgress, refreshStartupProgress } from './startupProgress';
import { CREEP_STALE_MS } from './screenProgress';
import { dismissUpdateScreen, EXIT_HOLD_MS, startUpdateScreen, stopUpdateScreen, tickUpdateScreen, updateScreenView } from './screenStore';
import { resetUpdateStoreForTests, updateConfirmed, updateStatus, updateUnreachable, type UpdateStatus } from './store';
import type { UpdateOperation } from './model';

type ShellWindow = Window & {
	__CORSIA_SHELL__?: unknown;
	__TAURI_INTERNALS__?: { invoke?: (command: string) => Promise<unknown> };
};

let progress: unknown;
let invoke: ReturnType<typeof vi.fn>;

function inFlight(operationId = 'update-1'): UpdateOperation {
	return { operation_id: operationId, kind: 'update', status: 'in_flight', phase: 'restarting', target_version: '2026.10.13' };
}

function status(operation: UpdateOperation): UpdateStatus {
	return {
		branch: 'dev', current_sha: 'old', remote_sha: 'new', commits_behind: 1, available: true,
		checked_at: 0, error: null, current_version: '2026.10.12', target_version: '2026.10.13', usual_seconds: 45,
		operation,
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(0);
	token.set('test-token');
	resetUpdateStoreForTests();
	clearStartupProgress();
	progress = null;
	invoke = vi.fn(async (command: string) => {
		expect(command).toBe('read_startup_progress');
		return progress;
	});
	const shell = window as ShellWindow;
	shell.__CORSIA_SHELL__ = { mode: 'local' };
	shell.__TAURI_INTERNALS__ = { invoke };
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })));
	startUpdateScreen();
});

afterEach(() => {
	stopUpdateScreen();
	clearStartupProgress();
	resetUpdateStoreForTests();
	token.set(null);
	const shell = window as ShellWindow;
	delete shell.__CORSIA_SHELL__;
	delete shell.__TAURI_INTERNALS__;
	vi.unstubAllGlobals();
	vi.clearAllTimers();
	vi.useRealTimers();
});

describe('update screen store', () => {
	it('keeps an advancing 245-second progress replay monotone, then stops creeping once its source freezes', async () => {
		updateStatus.set(status(inFlight()));
		tickUpdateScreen(0);
		updateUnreachable.set(true);
		let previousPercent = get(updateScreenView)!.percent;
		let lastDisplayedChangeAt = 0;
		let longestUnchangedDisplayedMs = 0;
		for (let at = 5_000, seq = 1; at <= 245_000; at += 5_000, seq += 1) {
			vi.setSystemTime(at);
			progress = { schema: 1, pid: 91, seq, phase: 'migrating', done: 2, total: 4 };
			await refreshStartupProgress();
			tickUpdateScreen(at);
			const alive = get(updateScreenView)!;
			expect(alive.mode).toBe('progress');
			expect(alive.percent).toBeGreaterThanOrEqual(previousPercent);
			if (alive.percent !== previousPercent) {
				longestUnchangedDisplayedMs = Math.max(longestUnchangedDisplayedMs, at - lastDisplayedChangeAt);
				lastDisplayedChangeAt = at;
			}
			previousPercent = alive.percent;
		}

		expect(longestUnchangedDisplayedMs).toBeLessThanOrEqual(CREEP_STALE_MS);
		expect(get(updateScreenView)).toMatchObject({ mode: 'progress', step: 'upgrading', fromVersion: '2026.10.12', toVersion: '2026.10.13' });
		const displayedWhileAlive = previousPercent;

		const creepStoppedAt = 245_000 + CREEP_STALE_MS;
		vi.setSystemTime(creepStoppedAt);
		await refreshStartupProgress();
		tickUpdateScreen(creepStoppedAt);
		expect(get(updateScreenView)).toMatchObject({ mode: 'progress', percent: displayedWhileAlive });

		const frozenAt = 245_000 + 60_000;
		vi.setSystemTime(frozenAt);
		await refreshStartupProgress();
		tickUpdateScreen(frozenAt);
		expect(get(updateScreenView)).toMatchObject({ mode: 'longer', step: 'upgrading', percent: displayedWhileAlive });
	});

	it('keeps a dismissed terminal outcome dismissed and completes only for the matching operation', () => {
		updateStatus.set(status(inFlight()));
		tickUpdateScreen(0);
		updateUnreachable.set(false);
		updateStatus.set(status({ ...inFlight(), status: 'failed', failure_code: 'fetch_failed', settled_at: '2026-10-13T00:00:00Z' }));
		updateConfirmed.set({ operationId: 'another-update', version: '2026.10.99' });
		tickUpdateScreen(1_000);
		expect(get(updateScreenView)?.mode).toBe('failed');
		updateConfirmed.set(null);

		dismissUpdateScreen();
		expect(get(updateScreenView)).toBeNull();
		tickUpdateScreen(2_000);
		expect(get(updateScreenView)).toBeNull();

		updateStatus.set(status(inFlight('update-2')));
		tickUpdateScreen(3_000);
		updateConfirmed.set({ operationId: 'another-update', version: '2026.10.99' });
		tickUpdateScreen(3_001);
		expect(get(updateScreenView)?.mode).not.toBe('done');

		updateConfirmed.set({ operationId: 'update-2', version: '2026.10.13' });
		tickUpdateScreen(3_002);
		expect(get(updateScreenView)).toMatchObject({ mode: 'done', percent: 100, toVersion: '2026.10.13' });
		vi.advanceTimersByTime(EXIT_HOLD_MS);
		tickUpdateScreen(4_003);
		expect(get(updateScreenView)).toBeNull();
	});
});
