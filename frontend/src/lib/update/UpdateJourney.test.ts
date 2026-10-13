import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import UpdateJourney from './UpdateJourney.svelte';
import {
	resetUpdateStoreForTests,
	updateStatus,
	updateSurface,
	updateUnreachable,
	type UpdateStatus,
} from './store';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function status(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
	return {
		branch: 'dev', current_sha: 'current', remote_sha: 'remote', commits_behind: 1,
		available: true, checked_at: 1, error: null, current_version: '1.0.0',
		target_version: '1.1.0', changes: {
			kind: 'changelog', headline: 'A quieter update', items: ['Faster startup'], more: 0,
		}, drain_cap_seconds: 90, usual_seconds: 45,
		...overrides,
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	resetUpdateStoreForTests();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	resetUpdateStoreForTests();
	vi.useRealTimers();
});

describe('UpdateJourney', () => {
	it('renders the decision sheet with release notes and the S3 actions', () => {
		updateStatus.set(status());
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();

		expect(host.textContent).toContain('Update CORSIA');
		expect(host.textContent).toContain('1.1.0');
		expect(host.textContent).toContain("What's new");
		expect(host.textContent).toContain('Faster startup');
		expect(host.textContent).toContain('Later');
		expect(host.textContent).toContain('Update now');
	});

	it('renders the four-step progress panel with the restarting-only note', () => {
		updateStatus.set(status({
			operation: {
				operation_id: 'update-1', kind: 'update', status: 'in_flight', phase: 'draining',
				drain_total: 2, blocking: 1, requested_at: new Date().toISOString(),
			},
		}));
		updateSurface.set('panel');
		updateUnreachable.set(true);
		component = mount(UpdateJourney, { target: host });
		flushSync();

		expect(host.textContent).toContain('Getting the update');
		expect(host.textContent).toContain('Stopping agents safely');
		expect(host.textContent).toContain('Restarting CORSIA');
		expect(host.textContent).toContain('Checking everything and resuming agents');
		expect(host.textContent).toContain('Restarting');
		expect(host.textContent).toContain('This page comes back on its own');
		expect(host.textContent).toContain("Reloading now shows a connection error until it's back.");
	});

	it('caps release notes and exposes the remaining changes', () => {
		updateStatus.set(status({ changes: { kind: 'changelog', headline: null, items: ['One', 'Two', 'Three', 'Four'], more: 2 } }));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain('Show all 6 changes');
		expect(host.textContent).not.toContain('Four');
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Show all'))?.click();
		flushSync();
		expect(host.textContent).toContain('Four');
	});

	it('renders refusal details only when the operation supplies them', () => {
		updateStatus.set(status({ operation: { operation_id: 'update-1', status: 'failed', failure_code: 'disk_space', failure_detail: '{"needed_bytes":12000000000,"free_bytes":0}', conductor_told_at: '2026-10-13T00:00:00Z', log_tail: 'failure log' } }));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain('Your workspace conductor has been told.');
		expect(host.textContent).toContain('Details');
		expect(host.textContent).toContain('failure log');
	});

	it('shows a read-only what is new sheet for a completed update', () => {
		updateStatus.set(status({
			available: false,
			operation: { operation_id: 'update-1', kind: 'update', status: 'complete', target_version: '1.1.0', settled_at: new Date().toISOString() },
		}));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain("What's new in 1.1.0");
		expect(host.textContent).toContain('installed today at');
		expect(host.textContent).toContain('Faster startup');
		expect(host.textContent).toContain('Done');
		expect(host.textContent).not.toContain('Update now');
	});

	it('offers a newer version instead of replaying an older failed operation', () => {
		updateStatus.set(status({
			target_version: '1.2.0',
			operation: { operation_id: 'update-0', kind: 'update', status: 'failed', target_version: '1.1.0', failure_code: 'disk_space', settled_at: '2026-10-01T00:00:00Z' },
		}));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain('Update CORSIA');
		expect(host.textContent).toContain('Update now');
		expect(host.textContent).not.toContain("Update didn't start");
	});
});
