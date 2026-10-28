import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { clearStartupProgress, refreshStartupProgress, startupProgress } from './startupProgress';
import { countUp, FLOOR_RATE, GAP_RATE } from './percentCountUp';
import { CREEP_STALE_MS, REMOTE_ESTIMATE_CEILING } from './screenProgress';
import { continueHandedOverScreen, dismissUpdateScreen, EXIT_HOLD_MS, handOverScreen, startUpdateScreen, stopUpdateScreen, tickUpdateScreen, updateScreenView } from './screenStore';
import { loadUpdateStatus, resetUpdateStoreForTests, updateConfirmed, updateStatus, updateUnreachable, type UpdateStatus } from './store';
import type { UpdateOperation } from './model';

type ShellWindow = Window & {
	__CORSIA_SHELL__?: unknown;
	__TAURI_INTERNALS__?: { invoke?: (command: string) => Promise<unknown> };
};

let progress: unknown;
let readerFails = false;
let invoke: (command: string) => Promise<unknown>;

function inFlight(operationId = 'update-1'): UpdateOperation {
	return { operation_id: operationId, kind: 'update', status: 'in_flight', phase: 'restarting', target_version: '2026.10.13' };
}

function status(operation: UpdateOperation, usualSeconds: number | null = 45): UpdateStatus {
	return {
		branch: 'dev', current_sha: 'old', remote_sha: 'new', commits_behind: 1, available: true,
		checked_at: 0, error: null, current_version: '2026.10.12', target_version: '2026.10.13', usual_seconds: usualSeconds,
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
	readerFails = false;
	invoke = vi.fn(async (command: string) => {
		expect(command).toBe('read_startup_progress');
		if (readerFails) throw new Error('reader unavailable');
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
	it('keeps a replacement visible when the previous operation had finished', () => {
		updateStatus.set(status(inFlight('a')));
		updateConfirmed.set({ operationId: 'a', version: '2026.10.13' });
		expect(get(updateScreenView)?.mode).toBe('done');
		updateStatus.set(status(inFlight('b')));
		expect(get(updateScreenView)?.operationId).toBe('b');
		vi.advanceTimersByTime(EXIT_HOLD_MS);
		expect(get(updateScreenView)?.operationId).toBe('b');
	});

	it('shares its existing clock with the shell reader without rereading on store notifications', () => {
		const read = vi.fn();
		const stop = startUpdateScreen(undefined, read);
		updateStatus.set(status(inFlight()));
		startupProgress.set({ file: null, readAt: 0, readError: null });
		expect(read).not.toHaveBeenCalled();
		vi.advanceTimersByTime(2_000);
		expect(read).toHaveBeenCalledTimes(2);
		startupProgress.set({ file: null, readAt: 2_000, readError: null });
		expect(read).toHaveBeenCalledTimes(2);
		stop();
		vi.advanceTimersByTime(2_000);
		expect(read).toHaveBeenCalledTimes(2);
	});
	it('follows a replacement migration after a stopping service still reports serving', async () => {
		updateStatus.set(status(inFlight()));
		updateUnreachable.set(true);
		progress = { schema: 1, pid: 90, seq: 500, phase: 'serving', done: null, total: null };
		await refreshStartupProgress();
		vi.setSystemTime(5_000);
		progress = { schema: 1, pid: 91, seq: 1, phase: 'migrating', done: 1, total: 3 };
		await refreshStartupProgress();
		expect(get(updateScreenView)).toMatchObject({ step: 'upgrading', mode: 'progress' });
		expect(get(updateScreenView)!.percent).toBeLessThan(68);
	});

	it('uses a late-phase heartbeat for liveness without treating it as a replacement step', async () => {
		updateStatus.set(status(inFlight()));
		updateUnreachable.set(true);
		for (let at = 0; at <= 90_000; at += 10_000) {
			vi.setSystemTime(at);
			progress = { schema: 1, pid: 90, seq: at / 10_000, phase: 'serving', done: null, total: null };
			await refreshStartupProgress();
			expect(get(updateScreenView)).toMatchObject({ step: 'restarting', mode: 'progress' });
		}
		vi.setSystemTime(150_000);
		await refreshStartupProgress();
		expect(get(updateScreenView)).toMatchObject({ step: 'restarting', mode: 'longer' });
	});

	it('ignores a late old-process serving record after the replacement begins migrating', async () => {
		updateStatus.set(status(inFlight()));
		updateUnreachable.set(true);
		progress = { schema: 1, pid: 91, seq: 1, phase: 'migrating', done: 1, total: 3 };
		await refreshStartupProgress();
		vi.setSystemTime(50_000);
		progress = { schema: 1, pid: 90, seq: 500, phase: 'serving', done: null, total: null };
		await refreshStartupProgress();
		expect(get(updateScreenView)).toMatchObject({ step: 'upgrading', mode: 'progress' });
		vi.setSystemTime(60_000);
		progress = { schema: 1, pid: 91, seq: 1, phase: 'migrating', done: 1, total: 3 };
		await refreshStartupProgress();
		expect(get(updateScreenView)).toMatchObject({ step: 'upgrading', mode: 'longer' });
		progress = { schema: 1, pid: 91, seq: 2, phase: 'migrating', done: 1, total: 3 };
		await refreshStartupProgress();
		expect(get(updateScreenView)).toMatchObject({ step: 'upgrading', mode: 'progress' });
	});

	it('keeps liveness when a later process is first seen recovering', async () => {
		updateStatus.set(status(inFlight()));
		updateUnreachable.set(true);
		progress = { schema: 1, pid: 91, seq: 1, phase: 'migrating', done: 1, total: 3 };
		await refreshStartupProgress();
		for (let at = 10_000; at <= 90_000; at += 10_000) {
			vi.setSystemTime(at);
			progress = { schema: 1, pid: 92, seq: at / 10_000, phase: 'recovering', done: null, total: null };
			await refreshStartupProgress();
			expect(get(updateScreenView)).toMatchObject({ step: 'upgrading', mode: 'progress' });
		}
		vi.setSystemTime(150_000);
		await refreshStartupProgress();
		expect(get(updateScreenView)).toMatchObject({ mode: 'longer' });
	});

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

	it('keeps a reader failure distinct from a validated absent record', async () => {
		updateStatus.set(status(inFlight()));
		tickUpdateScreen(0);
		updateUnreachable.set(true);
		readerFails = true;
		vi.setSystemTime(60_000);
		await refreshStartupProgress();
		tickUpdateScreen(60_000);
		expect(get(startupProgress)).toMatchObject({ file: null, readError: 'unavailable' });
		expect(get(updateScreenView)).toMatchObject({ mode: 'progress', estimated: false, startupProgressUnavailable: true });

		readerFails = false;
		await refreshStartupProgress();
		tickUpdateScreen(60_001);
		expect(get(startupProgress)).toMatchObject({ file: null, readError: null });
		expect(get(updateScreenView)).toMatchObject({ mode: 'longer', startupProgressUnavailable: false });
	});

	it('logs one sanitized reader failure until a successful read resets the bound', async () => {
		const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
		updateStatus.set(status(inFlight()));
		updateUnreachable.set(true);
		readerFails = true;
		await refreshStartupProgress();
		await refreshStartupProgress();
		expect(warning).toHaveBeenCalledTimes(1);
		expect(warning).toHaveBeenCalledWith('[update] startup progress is unavailable');

		readerFails = false;
		await refreshStartupProgress();
		readerFails = true;
		await refreshStartupProgress();
		expect(warning).toHaveBeenCalledTimes(2);
		warning.mockRestore();
	});

	it('does not invent a remote estimate without history and retains a known usual time', () => {
		const shell = window as ShellWindow;
		shell.__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio Mac' };
		updateStatus.set(status(inFlight(), null));
		tickUpdateScreen(0);
		updateUnreachable.set(true);
		tickUpdateScreen(120_000);
		expect(get(updateScreenView)).toMatchObject({ mode: 'progress', estimated: false });

		stopUpdateScreen();
		updateScreenView.set(null);
		updateStatus.set(status(inFlight(), 45));
		startUpdateScreen();
		tickUpdateScreen(0);
		updateStatus.set(status(inFlight(), null));
		updateUnreachable.set(true);
		tickUpdateScreen(1_000);
		expect(get(updateScreenView)).toMatchObject({ estimated: true });
	});

	it('counts a remote screen up across the restart and the handoff to real steps, never back and never in a jump', () => {
		const shell = window as ShellWindow;
		shell.__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio Mac' };
		updateStatus.set(status(inFlight('update-remote'), 45));
		tickUpdateScreen(0);
		const seen: number[] = [];
		const read = (at: number) => { tickUpdateScreen(at); seen.push(get(updateScreenView)?.mode === 'done' ? 100 : get(updateScreenView)?.percent ?? 0); };
		updateUnreachable.set(true);
		for (let at = 1_000; at <= 120_000; at += 1_000) read(at);
		expect(Math.max(...seen)).toBeLessThanOrEqual(REMOTE_ESTIMATE_CEILING);
		updateUnreachable.set(false);
		updateStatus.set(status({ ...inFlight('update-remote'), phase: 'resuming' }, 45));
		for (let at = 121_000; at <= 131_000; at += 1_000) read(at);
		updateStatus.set(status({ ...inFlight('update-remote'), phase: 'verifying' }, 45));
		for (let at = 132_000; at <= 136_000; at += 1_000) read(at);
		updateConfirmed.set({ operationId: 'update-remote', version: '2026.10.13' });
		read(137_000);
		expect(seen.at(-1)).toBe(100);
		for (let i = 1; i < seen.length; i += 1) expect(seen[i]).toBeGreaterThanOrEqual(seen[i - 1]);

		// What the screen draws: each reading becomes the target, and the number counts up to it frame by frame.
		let shown = seen[0];
		for (const target of seen) {
			for (let elapsed = 0; elapsed < 1_000; elapsed += 16) {
				const next = countUp(shown, target, 16);
				expect(next).toBeGreaterThanOrEqual(shown);
				expect(next - shown).toBeLessThanOrEqual(Math.max((target - shown) * GAP_RATE, FLOOR_RATE) * 0.016 + 1e-9);
				shown = next;
			}
			expect(shown).toBe(target);
		}
	});

	it.each(['fetch_failed', 'disk_space', 'launch_failed', null] as const)('keeps %s preparation or unknown failure out of the restart screen', (failureCode) => {
		updateStatus.set(status({ ...inFlight(), status: 'failed', failure_code: failureCode, settled_at: '2026-10-13T00:00:00Z' }));
		tickUpdateScreen(1_000);
		expect(get(updateScreenView)).toBeNull();
	});

	it('keeps post-restart failures on the restart screen and completes only for the matching operation', () => {
		updateStatus.set(status({ ...inFlight(), status: 'failed', failure_code: 'rolled_back', settled_at: '2026-10-13T00:00:00Z' }));
		tickUpdateScreen(1_000);
		expect(get(updateScreenView)?.mode).toBe('failed');

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

	it('dismisses its matching terminal screen and toast together', async () => {
		const response = status({ ...inFlight('update-1'), status: 'failed', failure_code: 'rolled_back', settled_at: '2026-10-13T00:00:00Z' });
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => response })));

		await loadUpdateStatus();
		expect(get(updateScreenView)?.mode).toBe('failed');
		expect(get(toasts).find((toast) => toast.pinned?.key === 'update')?.type).toBe('error');
		dismissUpdateScreen();
		expect(get(updateScreenView)).toBeNull();
		expect(get(toasts).find((toast) => toast.pinned?.key === 'update')).toBeUndefined();
	});

	it('does not dismiss a newer operation toast from a stale terminal screen', async () => {
		const first = status({ ...inFlight('update-1'), status: 'failed', failure_code: 'rolled_back', settled_at: '2026-10-13T00:00:00Z' });
		const next = status({ ...inFlight('update-2'), status: 'failed', failure_code: 'rolled_back', settled_at: '2026-10-13T00:01:00Z' });
		let response = first;
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => response })));

		await loadUpdateStatus();
		response = next;
		await loadUpdateStatus();
		expect(get(updateScreenView)?.mode).toBe('failed');
		expect(get(toasts).find((toast) => toast.pinned?.key === 'update')?.type).toBe('error');
		dismissUpdateScreen();
		expect(get(toasts).find((toast) => toast.pinned?.key === 'update')?.type).toBe('error');
	});

	describe('a screen handed between pages', () => {
		/** A new page: nothing carried over in memory, only what the handoff holds. */
		function freshPage(serviceDown: boolean): void {
			stopUpdateScreen();
			resetUpdateStoreForTests();
			clearStartupProgress();
			startUpdateScreen();
			updateUnreachable.set(serviceDown);
		}

		async function runningUpgrade(until: number, operationId = 'update-1'): Promise<number> {
			updateStatus.set(status(inFlight(operationId)));
			tickUpdateScreen(0);
			updateUnreachable.set(true);
			for (let at = 5_000, seq = 1; at <= until; at += 5_000, seq += 1) {
				vi.setSystemTime(at);
				progress = { schema: 1, pid: 91, seq, phase: 'migrating', done: 2, total: 4 };
				await refreshStartupProgress();
				tickUpdateScreen(at);
			}
			return get(updateScreenView)!.percent;
		}

		it('continues on the shell page at the same percentage, step, versions and stall clock', async () => {
			const shown = await runningUpgrade(120_000);
			const frozenFor = 50_000;
			vi.setSystemTime(120_000 + frozenFor);
			await refreshStartupProgress();
			tickUpdateScreen(120_000 + frozenFor);
			const before = get(updateScreenView)!;
			expect(before).toMatchObject({ mode: 'progress', step: 'upgrading' });
			const handoff = handOverScreen('light', '/');
			expect(handoff).not.toBeNull();

			// The shell page reads the progress record before it continues the screen, as shellEntry.ts does.
			freshPage(true);
			await refreshStartupProgress();
			continueHandedOverScreen(handoff!, true);
			expect(get(updateScreenView)).toMatchObject({ mode: 'progress', step: 'upgrading', percent: before.percent, fromVersion: '2026.10.12', toVersion: '2026.10.13', serviceDown: true });
			expect(before.percent).toBeGreaterThanOrEqual(shown);

			// The same frozen record on the shell page reaches "longer" when the original page would have, not 60 s later.
			const longerAt = 120_000 + 60_000;
			vi.setSystemTime(longerAt);
			await refreshStartupProgress();
			tickUpdateScreen(longerAt);
			expect(get(updateScreenView)).toMatchObject({ mode: 'longer', percent: before.percent });
		});

		it('keeps a latched launch failure across the shell handoff', () => {
			updateStatus.set(status(inFlight()));
			tickUpdateScreen(1_000);
			updateStatus.set(status({ ...inFlight(), status: 'failed', failure_code: 'launch_failed', settled_at: '2026-10-13T00:00:00Z' }));
			tickUpdateScreen(1_001);
			const handoff = handOverScreen('dark', '/');
			expect(handoff?.failure).toMatchObject({ code: 'launch_failed' });

			freshPage(true);
			continueHandedOverScreen(handoff!, true);
			expect(get(updateScreenView)).toMatchObject({ mode: 'failed', failure: { title: "Update didn't finish" }, serviceDown: true });
		});

		it('continues back on the service page only for the same operation, then confirms and leaves', async () => {
			await runningUpgrade(60_000);
			const percent = get(updateScreenView)!.percent;
			const handoff = handOverScreen('dark', '/');

			freshPage(false);
			continueHandedOverScreen(handoff!, false);
			expect(get(updateScreenView)).toBeNull();

			updateStatus.set(status({ ...inFlight('update-1'), phase: 'verifying' }));
			tickUpdateScreen(61_000);
			expect(get(updateScreenView)).toMatchObject({ step: 'verifying', fromVersion: '2026.10.12' });
			expect(get(updateScreenView)!.percent).toBeGreaterThanOrEqual(percent);

			updateConfirmed.set({ operationId: 'update-1', version: '2026.10.13' });
			tickUpdateScreen(62_000);
			expect(get(updateScreenView)).toMatchObject({ mode: 'done', percent: 100 });
		});

		it('drops a handoff for another operation or an outcome already shown', async () => {
			await runningUpgrade(10_000, 'update-3');
			const handoff = handOverScreen('dark', '/')!;

			freshPage(false);
			continueHandedOverScreen(handoff, false);
			updateStatus.set(status({ operation_id: 'update-9', kind: 'update', status: 'complete', target_version: '2026.10.13' }));
			tickUpdateScreen(11_000);
			expect(get(updateScreenView)).toBeNull();

			freshPage(false);
			continueHandedOverScreen(handoff, false);
			updateStatus.set(status({ operation_id: 'update-3', kind: 'update', status: 'complete', target_version: '2026.10.13', outcome_seen_at: '2026-10-24T21:00:00Z' }));
			tickUpdateScreen(12_000);
			expect(get(updateScreenView)).toBeNull();
		});
	});
});

describe('the shell’s ready address', () => {
	it('is kept beside the record, and alone does not count as a record', async () => {
		progress = { schema: 1, pid: 91, seq: 3, phase: 'migrating', done: 1, total: 4, ready_url: 'http://127.0.0.1:47895' };
		await refreshStartupProgress();
		expect(get(startupProgress)).toMatchObject({ file: { seq: 3 }, readyUrl: 'http://127.0.0.1:47895' });
		progress = { ready_url: 'http://127.0.0.1:47895' };
		await refreshStartupProgress();
		expect(get(startupProgress)).toMatchObject({ file: null, readyUrl: 'http://127.0.0.1:47895' });
	});

	it.each([null, { schema: 1, pid: 91, seq: 3, phase: 'migrating', done: 1, total: 4 }, { ready_url: 42 }, { ready_url: '' }])('is absent from %j', async (reading) => {
		progress = reading;
		await refreshStartupProgress();
		expect(get(startupProgress)?.readyUrl ?? null).toBeNull();
	});
});
