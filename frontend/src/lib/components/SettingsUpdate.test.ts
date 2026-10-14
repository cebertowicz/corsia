import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SettingsUpdate from './SettingsUpdate.svelte';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import {
	resetUpdateStoreForTests,
	updateStatus,
	updateSurface,
	type UpdateStatus,
} from '$lib/update/store';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function status(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
	return {
		branch: 'dev', current_sha: 'current', remote_sha: 'remote', commits_behind: 1,
		available: false, checked_at: 1, error: null, current_version: '1.0.0',
		...overrides,
	};
}

beforeEach(() => {
	resetUpdateStoreForTests();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	resetUpdateStoreForTests();
	token.set(null);
	vi.unstubAllGlobals();
});

describe('SettingsUpdate', () => {
	it('shows the longer same-day version the last update installed', () => {
		updateStatus.set(status({
			current_version: '2026.10.14',
			operation: { operation_id: 'update-1', kind: 'update', status: 'complete', target_version: '2026.10.14.0431', settled_at: '2026-10-14T01:33:58Z' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.querySelector('.service-version')?.textContent).toBe('2026.10.14.0431');
	});

	it('keeps the current version while offering the target update', () => {
		updateStatus.set(status({ available: true, target_version: '1.1.0' }));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('1.0.0');
		expect(host.textContent).toContain('1.1.0 available');
		expect(host.querySelector('.pill.active')?.textContent).toBe('1.1.0 available');
		expect(host.textContent).toContain('Update');
	});

	it('uses StatePill and hides the available version while updating', () => {
		updateStatus.set(status({
			available: true, target_version: '1.1.0',
			operation: { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase: 'draining' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.querySelector('.pill.active')).toBeNull();
		expect(host.textContent).not.toContain('1.1.0 available');
		expect(host.textContent).toContain('Updating to 1.1.0');
	});

	it('does not repeat the installed version as a last update', () => {
		updateStatus.set(status({
			operation: { operation_id: 'update-1', status: 'complete', target_version: '1.1.0' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).not.toContain('Last update');
		expect(host.textContent).not.toContain('Updated to 1.1.0');
	});

	it('keeps tonight in the Version pill and shows the plain automatic row', () => {
		updateStatus.set(status({
			available: true,
			schedule: { automatic: false, time: '03:00', tonight_at: '2026-10-14T03:00:00Z' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('1.0.0 · 3:00 AM tonight');
		expect(host.textContent).not.toContain('Scheduled for tonight');
		expect(host.textContent).toContain("Cancel tonight's update");
		expect(host.textContent).toContain('Keep CORSIA up to date automatically');
		expect(host.textContent).toContain('Installs at 3:00 AM.');
	});

	it('re-enables the automatic control after a failed schedule save', async () => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500 })));
		updateStatus.set(status({ schedule: { automatic: false, time: '03:00', tonight_at: null } }));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		const toggle = host.querySelector<HTMLButtonElement>('[aria-label="Automatically install updates overnight"]');
		toggle?.click();
		await vi.waitFor(() => expect(toggle?.disabled).toBe(false));

		expect(get(toasts).some((toast) => toast.message === "Couldn't save the schedule")).toBe(true);
	});

	it('keeps the refusal reason and its Details action', () => {
		updateStatus.set(status({
			operation: { operation_id: 'update-1', status: 'failed', failure_code: 'fetch_failed' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('CORSIA kept running and nothing was changed.');
		const details = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Details');
		expect(details).toBeDefined();
		details?.click();
		expect(get(updateSurface)).toBe('sheet');
	});
});
