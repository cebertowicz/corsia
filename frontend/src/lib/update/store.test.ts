import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import {
	loadUpdateStatus,
	releaseNotesFor,
	resetUpdateStoreForTests,
	startUpdate,
	updateFailureVerdict,
	updateInFlight,
	updateOperation,
	updateStarting,
	updateStatus,
	updateSurface,
	updateUnreachable,
} from './store';
import { rememberApprovedNotes } from './releaseNotes';

const running = (phase: 'holding' | 'steering' | 'draining' | 'ready' | 'restarting' | 'resuming') => ({
	branch: 'dev', current_sha: 'a', remote_sha: 'b', commits_behind: 1, available: true,
	checked_at: 1, error: null, current_version: '2026.10.12', target_version: '2026.10.13',
	drain_cap_seconds: 300, usual_seconds: null, server_time: '2026-10-13T22:00:00Z',
	operation: { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase, requested_at: '2026-10-13T22:00:00Z' },
});

beforeEach(() => {
	vi.useFakeTimers();
	token.set('test-token');
	resetUpdateStoreForTests();
	sessionStorage.clear();
	localStorage.clear();
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
	it('adopts the operation while the start request is still pending', async () => {
		let release: (value: unknown) => void = () => {};
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (String(url).includes('/api/system/restart')) {
				await new Promise((resolve) => { release = resolve; });
				return { ok: true, json: async () => ({ ok: true, operation_id: 'update-1' }) };
			}
			return { ok: true, json: async () => running('holding') };
		}));
		const started = startUpdate();
		expect(get(updateStarting)).toBe(true);
		await vi.advanceTimersByTimeAsync(1_100);
		expect(get(updateInFlight)).toBe(true);
		expect(get(updateStarting)).toBe(true);
		release(null);
		await started;
		expect(get(updateStarting)).toBe(false);
		expect(get(updateSurface)).toBe('panel');
	});

	it.each(['holding', 'steering', 'draining', 'ready', 'restarting', 'resuming'] as const)(
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
		expect(get(toasts).some((toast) => toast.message === 'An update is ready to install.')).toBe(true);
	});

	it('does not announce a version while an update is already running', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => running('draining') })));
		await loadUpdateStatus();
		expect(get(toasts).some((toast) => toast.message === 'An update is ready to install.')).toBe(false);
	});

	it('dismisses the failure toast when Details opens and does not restore it on close', async () => {
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
		expect(get(updateSurface)).toBe('sheet');

		updateSurface.set('closed');
		await loadUpdateStatus();
		expect(get(toasts)).toEqual([]);
	});

	it('keeps the approved change list when the server has already stopped offering it by the time the operation is adopted', async () => {
		const offered = { ...running('holding'), operation: undefined, changes: { kind: 'changelog', headline: null, items: ['One'], more: 0 } };
		updateStatus.set(offered as never);
		// The checkout already holds the update: the operation exists but no change list is offered any more.
		const adopted = { ...running('draining'), available: false };
		let release: (value: unknown) => void = () => {};
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (String(url).includes('/api/system/restart')) {
				await new Promise((resolve) => { release = resolve; });
				return { ok: true, json: async () => ({ ok: true }) };
			}
			return { ok: true, json: async () => adopted };
		}));
		const started = startUpdate();
		await vi.advanceTimersByTimeAsync(1_100);
		release(null);
		await started;
		const done = { ...adopted, operation: { ...adopted.operation, status: 'complete' } };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (String(url).startsWith('/api/version') ? { ok: false } : { ok: true, json: async () => done })));
		resetUpdateStoreForTests();
		await loadUpdateStatus();
		expect(releaseNotesFor(get(updateOperation), get(updateStatus))?.changes?.items).toEqual(['One']);
	});

	it('keeps the saved notes when a reloaded page adopts the running update without the offer', async () => {
		const offered = { ...running('holding'), changes: { kind: 'changelog', headline: null, items: ['One'], more: 0 } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => offered })));
		await loadUpdateStatus();
		resetUpdateStoreForTests();
		rememberApprovedNotes(null);
		const adopted = { ...running('draining'), available: false };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => adopted })));
		await loadUpdateStatus();
		const done = { ...adopted, operation: { ...adopted.operation, status: 'complete' } };
		resetUpdateStoreForTests();
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.startsWith('/api/version') ? { ok: false } : { ok: true, json: async () => done })));
		await loadUpdateStatus();
		expect(releaseNotesFor(get(updateOperation), get(updateStatus))?.changes?.items).toEqual(['One']);
	});

	it('keeps the notes of an update reachable after it completes and the offer clears', async () => {
		const offered = { ...running('holding'), changes: { kind: 'changelog', headline: null, items: ['One'], more: 0 } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => offered })));
		await loadUpdateStatus();
		resetUpdateStoreForTests();
		const done = { ...running('holding'), available: false, operation: { ...running('holding').operation, status: 'complete' } };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.startsWith('/api/version') ? { ok: false } : { ok: true, json: async () => done })));
		await loadUpdateStatus();
		expect(releaseNotesFor(get(updateOperation), get(updateStatus))?.changes?.items).toEqual(['One']);
	});

	it('pins a completed update once per browser, not once per tab', async () => {
		const hash = 'a'.repeat(40);
		const version = { frontend: { full_hash: hash }, backend: { full_hash: hash, current_head: { full_hash: hash } } };
		const done = { ...running('holding'), available: false, operation: { ...running('holding').operation, status: 'complete' } };
		const fetchMock = vi.fn(async (url: string) => (
			url.startsWith('/api/version') ? { ok: true, json: async () => version } : { ok: true, json: async () => done }
		));
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);
		expect(get(toasts).filter((toast) => toast.pinned?.key === 'update')).toHaveLength(1);

		for (const toast of get(toasts)) toasts.dismiss(toast.id);
		resetUpdateStoreForTests();
		sessionStorage.clear();
		fetchMock.mockClear();
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(10);
		expect(get(toasts).filter((toast) => toast.pinned?.key === 'update')).toHaveLength(0);
		expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/version'))).toBe(false);
	});

	it('does not re-judge the versions of a completed update that was already handled', async () => {
		localStorage.setItem('corsia:update:handled-operation-ids', JSON.stringify(['update-1']));
		const done = { ...running('holding'), available: false, operation: { ...running('holding').operation, status: 'complete' } };
		const fetchMock = vi.fn(async (url: string) => (
			url.startsWith('/api/version')
				? { ok: true, json: async () => ({ frontend: { full_hash: 'a' }, backend: { full_hash: 'b' } }) }
				: { ok: true, json: async () => done }
		));
		vi.stubGlobal('fetch', fetchMock);
		await loadUpdateStatus();
		await vi.advanceTimersByTimeAsync(15_000);
		expect(get(toasts).filter((toast) => toast.pinned?.key === 'update')).toHaveLength(0);
		expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/version'))).toBe(false);
	});

	it('records a refusal as its own verdict while an older failed operation is the latest row', async () => {
		const older = { ...running('holding'), operation: { operation_id: 'update-0', kind: 'update', status: 'failed', failure_code: 'fetch_failed', settled_at: '2026-10-01T00:00:00Z' } };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (
			String(url).includes('/api/system/restart')
				? { ok: false, json: async () => ({ detail: 'A restart is already in flight.' }) }
				: { ok: true, json: async () => older }
		)));
		await loadUpdateStatus();
		await startUpdate();
		expect(get(updateFailureVerdict)).toEqual({ kind: 'refused', message: 'A restart is already in flight.' });
		expect(get(updateSurface)).toBe('sheet');
	});

	it('opens the failed operation this request recorded instead of a verdict', async () => {
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
});
