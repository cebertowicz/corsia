import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SettingsUpdate from './SettingsUpdate.svelte';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import {
	resetUpdateStoreForTests,
	updateFailureVerdict,
	updateStatus,
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
		// The Version row carries the toast's words and a spinner, not a second phrasing.
		expect(host.textContent).toContain('Pausing agents');
		expect(host.querySelector('.updating .spinner')).toBeTruthy();
		expect(host.textContent).not.toContain('Updating to');
		expect(host.querySelector('.offer')).toBeNull();
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

	it('keeps tonight in the Version pill without claiming an automatic install', () => {
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
		expect(host.textContent).not.toContain('Installs at 3:00 AM.');
	});

	it('shows the install time only while automatic updates are on', () => {
		updateStatus.set(status({ schedule: { automatic: true, time: '03:30', tonight_at: null } }));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('Installs at 3:30 AM.');
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

	it('carries the failure itself in the Last update row, with Details for the raw reason and a retry', () => {
		updateStatus.set(status({
			operation: { operation_id: 'update-1', status: 'failed', failure_code: 'fetch_failed', error: 'network stopped', conductor_told_at: '2026-10-14T01:00:00Z' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('Last update');
		expect(host.textContent).toContain("Couldn't download the update.");
		expect(host.textContent).toContain('CORSIA kept running and nothing was changed.');
		expect(host.textContent).toContain('Your workspace conductor has been told.');
		expect(host.textContent).not.toContain('\u234c');
		const details = host.querySelector('details')!;
		expect(details.querySelector('summary')?.textContent?.trim()).toBe('Details');
		expect(details.querySelector('pre')?.textContent).toBe('network stopped.');
		const retry = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Try again');
		expect(retry).toBeDefined();
		expect(host.querySelector('.offer')).toBeNull();
	});

	it('shows a refusal the client found, ahead of an older stored failure', () => {
		updateStatus.set(status({ operation: { operation_id: 'update-0', status: 'failed', failure_code: 'fetch_failed' } }));
		updateFailureVerdict.set({ kind: 'refused', message: 'A restart is already in flight.' });
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('A restart is already in flight.');
		expect(host.textContent).not.toContain("Couldn't download the update.");
	});

	it('offers the update with its changes, one cost sentence, Update now and Tonight', () => {
		updateStatus.set(status({
			available: true, target_version: '1.1.0', usual_seconds: 60, agents_working: 2,
			changes: { kind: 'changelog', headline: null, items: ['Faster search', 'Calmer toasts', 'Clearer errors', 'Fewer restarts'], more: 21 },
			schedule: { automatic: false, time: '03:00', tonight_at: null },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		const offer = host.querySelector('.offer')!;
		expect(offer.querySelector('h4')?.textContent).toBe("What's new");
		expect(offer.querySelectorAll('li')).toHaveLength(3);
		expect(offer.textContent).toContain('CORSIA restarts for about a minute; 2 working agents pause and pick up where they left off.');
		const labels = Array.from(offer.querySelectorAll('button')).map((button) => button.textContent?.trim());
		expect(labels).toContain('Update now');
		expect(labels).toContain('Tonight at 3:00 AM');
		expect(labels).toContain('Show all 25');

		Array.from(offer.querySelectorAll<HTMLButtonElement>('button')).find((button) => button.textContent?.trim() === 'Show all 25')!.click();
		flushSync();
		expect(offer.querySelectorAll('li')).toHaveLength(4);
	});

	it('headlines a development build\'s commit titles as Recent changes without lane tags or PR numbers', () => {
		updateStatus.set(status({
			available: true, target_version: '2026.10.14.0431',
			changes: { kind: 'commits', headline: null, items: ['[Infrastructure] fix the lock message (#123)'], more: 0 },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.querySelector('.offer h4')?.textContent).toBe('Recent changes');
		expect(host.querySelector('.offer li')?.textContent).toBe('Fix the lock message');
	});

	it('lists the notes of an update that just installed, and names an overnight one', () => {
		updateStatus.set(status({
			current_version: '1.1.0',
			operation: {
				operation_id: 'update-1', kind: 'update', trigger: 'tonight', status: 'complete', target_version: '1.1.0',
				settled_at: new Date().toISOString(), changes: { kind: 'changelog', headline: null, items: ['Faster search'], more: 0 },
			},
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.querySelector('.notes h4')?.textContent).toBe("What's new in 1.1.0");
		expect(host.querySelector('.notes li')?.textContent).toBe('Faster search');
		expect(host.querySelector('.installed')?.textContent).toMatch(/^Updated overnight at /);
	});

	it('keeps Update now and offers Cancel tonight when tonight is scheduled', () => {
		updateStatus.set(status({
			available: true, target_version: '1.1.0',
			schedule: { automatic: false, time: '03:00', tonight_at: '2026-10-14T03:00:00Z' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		const labels = Array.from(host.querySelectorAll('.offer button')).map((button) => button.textContent?.trim());
		expect(labels).toContain('Update now');
		expect(labels).toContain("Cancel tonight's update");
		expect(labels).not.toContain('Tonight at 3:00 AM');
	});

	it('schedules tonight and offers Undo', async () => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ automatic: false, time: '03:00', tonight_at: '2026-10-14T03:00:00Z' }) })));
		updateStatus.set(status({ available: true, target_version: '1.1.0', schedule: { automatic: false, time: '03:00', tonight_at: null } }));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('.offer button')).find((button) => button.textContent?.trim() === 'Tonight at 3:00 AM')!.click();
		await vi.waitFor(() => expect(get(toasts).some((toast) => toast.message === 'Update scheduled for 3:00 AM tonight.')).toBe(true));
		expect(get(toasts).find((toast) => toast.action?.label === 'Undo')).toBeDefined();
	});

	it('drops the failed pill once a restart has activated the staged update', () => {
		const failed = { operation_id: 'update-1', kind: 'update', status: 'failed', failure_code: 'restart_pending', settled_at: '2026-10-14T01:33:58Z' } as const;
		updateStatus.set(status({ operation: { ...failed } }));
		component = mount(SettingsUpdate, { target: host });
		flushSync();
		expect(host.textContent).toContain("Update didn't start");
		unmount(component);

		updateStatus.set(status({ operation: { ...failed, activated_by_restart: true } }));
		component = mount(SettingsUpdate, { target: host });
		flushSync();
		expect(host.textContent).not.toContain("Update didn't start");
		expect(host.textContent).not.toContain('Try again');
		expect(host.textContent).not.toContain('Installed');
	});
});
