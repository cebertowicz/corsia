import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import {
	loadUpdateStatus,
	releaseNotesFor,
	resetUpdateStoreForTests,
	startUpdate,
	updateInFlight,
	updateOperation,
	updateStarting,
	updateStatus,
	updateSurface,
	updateUnreachable,
} from './store';

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
});
