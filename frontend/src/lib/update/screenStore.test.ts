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
	it('follows an advancing progress replay, then freezes after its longest 245-second unchanged interval', async () => {
		updateStatus.set(status(inFlight()));
		tickUpdateScreen(0);
		updateUnreachable.set(true);
		const replay = [
			{ at: 1_000, seq: 4, phase: 'migrating', done: 2, total: 4 },
			{ at: 20_000, seq: 5, phase: 'migrating', done: 3, total: 4 },
			{ at: 70_000, seq: 6, phase: 'validating', done: null, total: null },
			{ at: 140_000, seq: 7, phase: 'serving', done: null, total: null },
		] as const;
		const steps: string[] = [];
		for (const sample of replay) {
			vi.setSystemTime(sample.at);
			progress = { schema: 1, pid: 91, ...sample, version: 'not-an-app-version' };
			await refreshStartupProgress();
			tickUpdateScreen(sample.at);
			steps.push(get(updateScreenView)!.step);
		}

		const alive = get(updateScreenView)!;
		expect(invoke).toHaveBeenCalledTimes(replay.length);
		expect(steps).toEqual(['upgrading', 'upgrading', 'checking_data', 'starting']);
		expect(alive).toMatchObject({ mode: 'progress', fromVersion: '2026.10.12', toVersion: '2026.10.13' });
		expect(alive.fromVersion).not.toBe('1');
		const displayedWhileAlive = alive.percent;
		const longestUnchangedDisplayedMs = 245_000;

		const frozenAt = replay[replay.length - 1].at + longestUnchangedDisplayedMs;
		vi.setSystemTime(frozenAt);
		await refreshStartupProgress();
		tickUpdateScreen(frozenAt);
		const frozen = get(updateScreenView)!;
		expect(longestUnchangedDisplayedMs).toBeGreaterThan(CREEP_STALE_MS);
		expect(frozen).toMatchObject({ mode: 'longer', step: 'starting', percent: displayedWhileAlive });
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
