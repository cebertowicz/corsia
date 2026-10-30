import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { toasts } from '$lib/stores/toast';
import UpdateProgressToast from './UpdateProgressToast.svelte';
import { resetUpdateStoreForTests, updateProgressMinimized, updateStarting, updateStatus, updateToastSuppressed, type UpdateStatus } from './store';

let component: Record<string, unknown> | null = null;

const status = (operation: UpdateStatus['operation']): UpdateStatus => ({
	branch: 'dev', current_sha: 'a', remote_sha: 'b', commits_behind: 1, available: true, checked_at: 1, error: null,
	current_version: '1.0.0', target_version: '1.1.0', server_time: '2026-10-14T20:00:12Z', operation,
});
const running = (phase: 'holding' | 'draining' | 'restarting') =>
	status({ operation_id: 'update-1', kind: 'update', status: 'in_flight', phase, requested_at: '2026-10-14T20:00:00Z', drain_total: 3, blocking: 2 });
const pinned = () => get(toasts).find((toast) => toast.pinned?.key === 'update');

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(new Date('2026-10-14T20:00:12Z'));
	resetUpdateStoreForTests();
	component = mount(UpdateProgressToast, { target: document.body });
});

afterEach(() => {
	unmount(component!);
	component = null;
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	resetUpdateStoreForTests();
	vi.useRealTimers();
});

describe('UpdateProgressToast', () => {
	it('pins nothing while no update runs', () => {
		flushSync();
		expect(pinned()).toBeUndefined();
	});

	it('shows progress while the start request is still pending', () => {
		updateStarting.set(true);
		flushSync();
		expect(pinned()).toMatchObject({ type: 'progress', message: 'Installing update', pinned: { time: '0:00' } });
	});

	it('pins the step phrase and elapsed time, closed by a minimize chevron and without an action', async () => {
		updateStatus.set(running('draining'));
		flushSync();
		const toast = pinned()!;
		expect(toast).toMatchObject({ type: 'progress', message: 'Pausing agents · 1 of 3' });
		expect(toast.pinned).toMatchObject({ time: '0:12', closeIcon: 'minimize', closeLabel: 'Minimize progress', compact: undefined });
		expect(toast.action).toBeUndefined();

		await vi.advanceTimersByTimeAsync(3_000);
		expect(pinned()?.pinned?.time).toBe('0:15');
		expect(pinned()?.id).toBe(toast.id);
	});

	it('shrinks to a pill that expands again, keeping one toast', () => {
		updateStatus.set(running('draining'));
		flushSync();
		const id = pinned()!.id;

		pinned()!.pinned!.onClose!();
		flushSync();
		expect(get(updateProgressMinimized)).toBe(true);
		expect(pinned()).toMatchObject({ id, message: 'Updating', pinned: { compact: true, expandLabel: 'Show update progress' } });

		pinned()!.pinned!.onExpand!();
		flushSync();
		expect(get(updateProgressMinimized)).toBe(false);
		expect(pinned()).toMatchObject({ id, message: 'Pausing agents · 1 of 3' });
	});

	it('says Restarting CORSIA once the server is unreachable mid-update', () => {
		updateStatus.set(running('restarting'));
		flushSync();
		expect(pinned()?.message).toBe('Restarting CORSIA');
	});

	it('removes only the update-progress toast while System Updates is visible and restores it elsewhere', () => {
		updateStatus.set(running('draining'));
		flushSync();
		expect(pinned()?.type).toBe('progress');
		updateToastSuppressed.set(true);
		flushSync();
		expect(pinned()).toBeUndefined();
		updateToastSuppressed.set(false);
		flushSync();
		expect(pinned()?.type).toBe('progress');
	});
});
