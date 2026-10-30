import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { settingsNavigation } from '$lib/stores/settingsNavigation';
import {
	loadUpdateStatus,
	resetUpdateStoreForTests,
	saveUpdateSchedule,
	startUpdate,
	updateFailureVerdict,
	updateInFlight,
	updateOperation,
	updateStarting,
	updateProgressMinimized,
	updateStatus,
	updateToastSuppressed,
	updateUnreachable,
} from './store';

const running = (phase: 'holding' | 'steering' | 'draining' | 'ready' | 'restarting' | 'resuming' | 'verifying') => ({
	branch: 'dev', current_sha: 'a', remote_sha: 'b', commits_behind: 1, available: true,
	checked_at: 1, error: null, current_version: '2026.10.12', target_version: '2026.10.13',
	drain_cap_seconds: 300, usual_seconds: null, server_time: '2026-10-13T22:00:00Z',
	operation: { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase, requested_at: '2026-10-13T22:00:00Z' },
});

const SEEN_URL = '/api/system/update-operations/update-1/outcome-seen';
const hash = 'a'.repeat(40);
const sameVersions = { frontend: { full_hash: hash }, backend: { full_hash: hash, current_head: { full_hash: hash } } };
const otherVersions = { frontend: { full_hash: 'a' }, backend: { full_hash: 'b' } };
const completed = (extra: Record<string, unknown> = {}) => ({ ...running('holding'), available: false, operation: { ...running('holding').operation, status: 'complete', ...extra } });
const failedRow = (extra: Record<string, unknown> = {}) => ({ ...running('holding'), operation: { operation_id: 'update-1', kind: 'update', status: 'failed', failure_code: 'fetch_failed', settled_at: '2026-10-13T22:01:00Z', ...extra } });
const pinned = () => get(toasts).filter((toast) => toast.pinned?.key === 'update');
const versionReads = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/version')).length;

/** One fake server whose status can change between reads; `seen` lists the outcome-seen requests it received. */
function backend(state: { status: unknown; versions?: unknown }) {
	const seen: string[] = [];
	const fetchMock = vi.fn(async (url: string) => {
		const path = String(url);
		if (path.includes('/outcome-seen')) { seen.push(path); return { ok: true, json: async () => ({}) }; }
		if (path.startsWith('/api/version')) return { ok: true, json: async () => state.versions ?? sameVersions };
		return { ok: true, json: async () => state.status };
	});
	return { fetchMock, seen };
}

/** Another browser tab: its own copy of the store and the toast stack, sharing only the server. */
async function freshTab() {
	vi.resetModules();
	const { token: tabToken } = await import('$lib/stores/auth');
	tabToken.set('test-token');
	return { toasts: (await import('$lib/stores/toast')).toasts, store: await import('./store') };
}

beforeEach(() => {
	vi.useFakeTimers();
	token.set('test-token');
	resetUpdateStoreForTests();
});

afterEach(() => {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	resetUpdateStoreForTests();
	token.set(null);
	vi.unstubAllGlobals();
	vi.clearAllTimers();
	vi.useRealTimers();
});

describe('update store', () => {
	it('persists Tonight and adopts the returned server schedule', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
			expect(url).toContain('/api/system/update-schedule');
			expect(init?.method).toBe('PUT');
			return { ok: true, json: async () => ({ automatic: false, time: '03:00', tonight_at: '2026-10-14T03:00:00+00:00' }) };
		}));

		expect(await saveUpdateSchedule({ tonight: true })).toBe(true);
		expect(get(updateStatus)).toBeNull();
	});

	it('shows a retry toast when a schedule save fails', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500 })));

		expect(await saveUpdateSchedule({ automatic: true })).toBe(false);
		const failure = get(toasts).find((toast) => toast.message === "Couldn't save the schedule");
		expect(failure?.action?.label).toBe('Retry');
	});

	it('adopts the operation while the start request is still pending', async () => {
		let release: (value: unknown) => void = () => {};
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (String(url).includes('/api/system/restart')) {
				await new Promise((resolve) => { release = resolve; });
				return { ok: true, json: async () => ({ ok: true, operation_id: 'update-1' }) };
			}
			return { ok: true, json: async () => running('holding') };
		}));
		const opened = get(settingsNavigation).openRequest;
		const started = startUpdate();
		expect(get(updateStarting)).toBe(true);
		await vi.advanceTimersByTimeAsync(1_100);
		expect(get(updateInFlight)).toBe(true);
		expect(get(updateStarting)).toBe(true);
		release(null);
		await started;
		expect(get(updateStarting)).toBe(false);
		// Progress is the pinned toast alone: starting opens no sheet, panel or Settings.
		expect(get(settingsNavigation).openRequest).toBe(opened);
	});

	it('adopts a new in-flight update when the start request loses its connection', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.includes('/api/system/restart')) throw new TypeError('Load failed');
			return { ok: true, json: async () => running('holding') };
		}));

		await startUpdate();

		expect(get(updateOperation)?.operation_id).toBe('update-1');
		expect(get(updateInFlight)).toBe(true);
		expect(get(updateFailureVerdict)).toBeNull();
		expect(pinned().filter((toast) => toast.type === 'error')).toEqual([]);
	});

	it('keeps the error toast when a failed start has no new operation', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.includes('/api/system/restart')) throw new TypeError('Load failed');
			return { ok: true, json: async () => ({ ...running('holding'), operation: null }) };
		}));

		await startUpdate();

		expect(get(updateFailureVerdict)?.kind).toBe('refused');
		expect(pinned()).toEqual([expect.objectContaining({ type: 'error', message: "Update didn't start" })]);
	});

	it('does not adopt an operation that was already in flight before the click', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.includes('/api/system/restart')) throw new TypeError('Load failed');
			return { ok: true, json: async () => running('holding') };
		}));
		await loadUpdateStatus();

		await startUpdate();

		expect(get(updateOperation)?.operation_id).toBe('update-1');
		expect(get(updateFailureVerdict)?.kind).toBe('refused');
		expect(pinned()).toEqual([expect.objectContaining({ type: 'error', message: "Update didn't start" })]);
	});

	it('refreshes a completed update into the restarted server status', async () => {
		const stale = { ...completed(), available: true, current_sha: 'old', remote_sha: 'installed', commits_behind: 1 };
		const current = { ...stale, available: false, current_sha: 'installed', remote_sha: 'installed', commits_behind: 0 };
		let statusReads = 0;
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.startsWith('/api/version')) return { ok: true, json: async () => sameVersions };
			if (url.includes('/outcome-seen')) return { ok: true, json: async () => ({}) };
			statusReads += 1;
			return { ok: true, json: async () => (statusReads === 1 ? stale : current) };
		}));

		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);

		expect(get(updateStatus)?.available).toBe(false);
	});


	it.each(['holding', 'steering', 'draining', 'ready', 'restarting', 'resuming', 'verifying'] as const)(
		'adopts the durable %s phase on load', async (phase) => {
			vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => running(phase) })));
			await loadUpdateStatus();
			expect(get(updateOperation)?.phase).toBe(phase);
			expect(get(updateInFlight)).toBe(true);
		},
	);

	it('adopts an in-flight operation after a reload without relying on local state', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => running('draining') })));
		await loadUpdateStatus();
		resetUpdateStoreForTests();
		await loadUpdateStatus();
		expect(get(updateOperation)?.operation_id).toBe('update-1');
		expect(get(updateOperation)?.phase).toBe('draining');
	});

	it('holds the restarting state while the status route is unreachable', async () => {
		let statusCalls = 0;
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.startsWith('/api/version')) return { ok: false };
			statusCalls += 1;
			if (statusCalls === 1) return { ok: true, json: async () => running('restarting') };
			throw new Error('offline');
		}));
		await loadUpdateStatus();
		await loadUpdateStatus();
		expect(get(updateUnreachable)).toBe(true);
		expect(get(updateOperation)?.phase).toBe('restarting');
		expect(get(updateStatus)?.operation?.status).toBe('in_flight');
	});

	it('still announces a new version after an earlier update settled', async () => {
		const settled = { ...running('resuming'), operation: { operation_id: 'update-0', kind: 'update', status: 'complete', target_version: '2026.10.12', settled_at: '2026-10-12T22:00:00Z' } };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.startsWith('/api/version') ? { ok: false } : { ok: true, json: async () => settled })));
		await loadUpdateStatus();
		const notice = get(toasts).find((toast) => toast.message === 'Update ready. Open Updates');
		expect(notice?.button).toBe(true);
		expect(notice?.pinned).toBeUndefined();
	});

	it('holds the update-ready notice while System Updates is visible, then shows it elsewhere', async () => {
		const settled = { ...running('resuming'), operation: { operation_id: 'update-0', kind: 'update', status: 'complete', target_version: '2026.10.12', settled_at: '2026-10-12T22:00:00Z' } };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.startsWith('/api/version') ? { ok: false } : { ok: true, json: async () => settled })));
		updateToastSuppressed.set(true);
		await loadUpdateStatus();
		expect(get(toasts).some((toast) => toast.message === 'Update ready. Open Updates')).toBe(false);
		updateToastSuppressed.set(false);
		expect(get(toasts).some((toast) => toast.message === 'Update ready. Open Updates')).toBe(true);
	});

	it('restores a ready notice after System hides it, but not after the person opens it', async () => {
		const settled = { ...running('resuming'), operation: { operation_id: 'update-0', kind: 'update', status: 'complete', target_version: '2026.10.12', settled_at: '2026-10-12T22:00:00Z' } };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.startsWith('/api/version') ? { ok: false } : { ok: true, json: async () => settled })));
		await loadUpdateStatus();
		expect(get(toasts).some((toast) => toast.message === 'Update ready. Open Updates')).toBe(true);

		updateToastSuppressed.set(true);
		expect(get(toasts).some((toast) => toast.message === 'Update ready. Open Updates')).toBe(false);
		updateToastSuppressed.set(false);
		const restored = get(toasts).find((toast) => toast.message === 'Update ready. Open Updates')!;
		expect(restored).toBeDefined();

		await toasts.runAction(restored.id);
		updateToastSuppressed.set(true);
		updateToastSuppressed.set(false);
		expect(get(toasts).some((toast) => toast.message === 'Update ready. Open Updates')).toBe(false);
	});

	it('opens Settings at the Updates card from the notice', async () => {
		const settled = { ...running('resuming'), operation: { operation_id: 'update-0', kind: 'update', status: 'complete', target_version: '2026.10.12', settled_at: '2026-10-12T22:00:00Z' } };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.startsWith('/api/version') ? { ok: false } : { ok: true, json: async () => settled })));
		const opened = get(settingsNavigation).openRequest;
		await loadUpdateStatus();
		const notice = get(toasts).find((toast) => toast.button)!;
		await toasts.runAction(notice.id);
		expect(get(settingsNavigation).section).toBe('system');
		expect(get(settingsNavigation).anchor).toBe('update');
		expect(get(settingsNavigation).openRequest).toBe(opened + 1);
		expect(get(toasts).some((toast) => toast.button)).toBe(false);
	});

	it('does not announce a version while an update is already running', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => running('draining') })));
		await loadUpdateStatus();
		expect(get(toasts).some((toast) => toast.message === 'Update ready. Open Updates')).toBe(false);
	});

	it('dismisses the failure toast when Details opens the Updates card and does not restore it', async () => {
		const failed = {
			...running('draining'),
			operation: { operation_id: 'update-1', kind: 'update', status: 'failed', phase: 'draining', error: 'network stopped' },
		};
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => failed })));

		await loadUpdateStatus();
		const toast = get(toasts).find((item) => item.pinned?.key === 'update');
		expect(toast?.action?.label).toBe('Details');
		await toasts.runAction(toast!.id);
		expect(get(toasts)).toEqual([]);
		expect(get(settingsNavigation).anchor).toBe('update');

		await loadUpdateStatus();
		expect(get(toasts)).toEqual([]);
	});

	it('shows an unseen completed update once per page and marks its outcome seen', async () => {
		const { fetchMock, seen } = backend({ status: completed() });
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);
		expect(pinned().map((toast) => toast.message)).toEqual(['Updated to 2026.10.13']);
		expect(seen).toEqual([SEEN_URL]);

		for (const toast of get(toasts)) toasts.dismiss(toast.id);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);
		expect(pinned()).toEqual([]);
		expect(seen).toEqual([SEEN_URL]);
	});

	it('shows nothing on a new device for an outcome another device already showed', async () => {
		const { fetchMock, seen } = backend({ status: completed({ outcome_seen_at: '2026-10-13T22:05:00Z' }), versions: otherVersions });
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(15_000);
		expect(pinned()).toEqual([]);
		expect(get(updateFailureVerdict)).toBeNull();
		expect(seen).toEqual([]);
		expect(versionReads(fetchMock)).toBe(0);
	});

	it('judges the running versions of an unseen completed update once per page', async () => {
		const { fetchMock, seen } = backend({ status: completed(), versions: otherVersions });
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(12_000);
		expect(get(updateFailureVerdict)?.kind).toBe('version_mismatch');
		expect(seen).toEqual([SEEN_URL]);
		const reads = versionReads(fetchMock);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(15_000);
		expect(versionReads(fetchMock)).toBe(reads);
		expect(seen).toEqual([SEEN_URL]);
	});

	it('keeps a failure visible until someone dismisses it, then stops showing it on every device', async () => {
		const state = { status: failedRow() };
		const { fetchMock, seen } = backend(state);
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		const toast = pinned()[0];
		expect(toast?.type).toBe('error');
		expect(seen).toEqual([]);
		toast.pinned!.onClose!();
		expect(seen).toEqual([SEEN_URL]);

		resetUpdateStoreForTests();
		for (const item of get(toasts)) toasts.dismiss(item.id);
		state.status = failedRow({ outcome_seen_at: '2026-10-13T22:06:00Z' });
		await loadUpdateStatus();
		expect(pinned()).toEqual([]);
	});

	it('lets a second tab drop its progress toast once the first tab handled the outcome', async () => {
		const state: { status: unknown } = { status: running('resuming') };
		const { fetchMock } = backend(state);
		vi.stubGlobal('fetch', fetchMock);
		const tabA = await freshTab();
		const tabB = await freshTab();
		await tabA.store.loadUpdateStatus();
		await tabB.store.loadUpdateStatus();
		for (const tab of [tabA, tabB]) tab.toasts.pin('update', { type: 'progress', message: 'Resuming 2 agents' });

		state.status = completed();
		await tabA.store.loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);
		expect(get(tabA.toasts).map((toast) => toast.message)).toEqual(['Updated to 2026.10.13']);

		state.status = completed({ outcome_seen_at: '2026-10-13T22:05:00Z' });
		await tabB.store.loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);
		expect(get(tabB.toasts)).toEqual([]);
	});

	it('keeps the outcome toast of the same tab when a later status read finds it seen', async () => {
		const state = { status: completed() };
		const { fetchMock } = backend(state);
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);
		state.status = completed({ outcome_seen_at: '2026-10-13T22:05:00Z' });
		await loadUpdateStatus();
		expect(pinned().map((toast) => toast.message)).toEqual(['Updated to 2026.10.13']);
	});

	it('records a refusal as its own verdict while an older failed operation is the latest row', async () => {
		const older = { ...running('holding'), operation: { operation_id: 'update-0', kind: 'update', status: 'failed', failure_code: 'fetch_failed', settled_at: '2026-10-01T00:00:00Z' } };
		const fetchMock = vi.fn(async (url: string) => (
			String(url).includes('/api/system/restart')
				? { ok: false, json: async () => ({ detail: 'A restart is already in flight.' }) }
				: { ok: true, json: async () => older }
		));
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		await startUpdate();
		expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/system/update-operations/update-0/outcome-seen')).toBe(true);
		expect(get(updateFailureVerdict)).toEqual({ kind: 'refused', message: 'A restart is already in flight.' });
		expect(pinned()[0]).toMatchObject({ message: "Update didn't start", type: 'error', action: { label: 'Details' } });
	});

	it('leaves the failed operation this request recorded to its own toast instead of a verdict', async () => {
		const before = { ...running('holding'), operation: { operation_id: 'update-0', kind: 'update', status: 'complete', settled_at: '2026-10-01T00:00:00Z' } };
		const recorded = { ...running('holding'), operation: { operation_id: 'update-1', kind: 'update', status: 'failed', failure_code: 'disk_space', settled_at: '2026-10-13T22:00:00Z' } };
		let statusReads = 0;
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (String(url).includes('/api/system/restart')) return { ok: false, json: async () => ({ detail: 'Not enough disk space.' }) };
			statusReads += 1;
			return { ok: true, json: async () => (statusReads === 1 ? before : recorded) };
		}));
		await loadUpdateStatus();
		await startUpdate();
		expect(get(updateFailureVerdict)).toBeNull();
		expect(get(updateOperation)?.operation_id).toBe('update-1');
	});

	it('shows no failure for a staged update that a restart has since activated', async () => {
		const row = { failure_code: 'restart_pending', operation_id: 'update-1' };
		const state = { status: failedRow({ ...row, activated_by_restart: true }) };
		const { fetchMock, seen } = backend(state);
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		expect(pinned()).toEqual([]);

		resetUpdateStoreForTests();
		state.status = failedRow({ ...row });
		await loadUpdateStatus();
		expect(pinned()[0]?.type).toBe('error');
		expect(seen).toEqual([]);
	});

	it('shows the same quiet state in a second tab', async () => {
		const state = { status: failedRow({ failure_code: 'restart_pending', activated_by_restart: true }) };
		const { fetchMock } = backend(state);
		vi.stubGlobal('fetch', fetchMock);
		const tabA = await freshTab();
		const tabB = await freshTab();
		await tabA.store.loadUpdateStatus();
		await tabB.store.loadUpdateStatus();
		for (const tab of [tabA, tabB]) expect(get(tab.toasts).filter((toast) => toast.type === 'error' || toast.pinned?.key === 'update')).toEqual([]);
	});

	describe('outcome toasts', () => {
		it.each([
			['a manual release update', {}, 'Updated to 2026.10.13', "What's new"],
			['an update that ran tonight', { trigger: 'tonight' }, 'Updated overnight to 2026.10.13', "What's new"],
			['an automatic update', { trigger: 'automatic' }, 'Updated overnight to 2026.10.13', "What's new"],
			['a development build', { changes: { kind: 'commits', headline: null, items: ['Tidy'], more: 0 } }, 'Updated to 2026.10.13', 'See changes'],
		])('words %s', async (_name, extra, message, label) => {
			const { fetchMock } = backend({ status: completed(extra) });
			vi.stubGlobal('fetch', fetchMock);
			await loadUpdateStatus();
			await vi.advanceTimersByTimeAsync(10);
			expect(pinned()[0]).toMatchObject({ message, type: 'success', action: { label } });
		});

		it('opens the Updates card from the success toast', async () => {
			const { fetchMock } = backend({ status: completed() });
			vi.stubGlobal('fetch', fetchMock);
			await loadUpdateStatus();
			await vi.advanceTimersByTimeAsync(10);
			await toasts.runAction(pinned()[0].id);
			expect(get(settingsNavigation).anchor).toBe('update');
		});
	});

	it('forgets a minimized progress toast once the operation settles', async () => {
		const state = { status: running('draining') as unknown };
		const { fetchMock } = backend(state);
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		updateProgressMinimized.set(true);
		await loadUpdateStatus();
		expect(get(updateProgressMinimized)).toBe(true);

		state.status = completed({ outcome_seen_at: '2026-10-13T22:05:00Z' });
		await loadUpdateStatus();
		expect(get(updateProgressMinimized)).toBe(false);
	});
});
