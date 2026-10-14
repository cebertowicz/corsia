import { get } from 'svelte/store';
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ToastContainer from '$lib/components/ToastContainer.svelte';
import { toasts } from '$lib/stores/toast';
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
	it('keeps the progress toast closed for the rest of its operation, and still shows what settles it', async () => {
		const running = (phase: string) => status({ available: false, operation: { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase, requested_at: new Date().toISOString() } });
		const progress = () => get(toasts).filter((toast) => toast.pinned?.key === 'update' && toast.type === 'progress');
		updateStatus.set(running('draining'));
		updateSurface.set('closed');
		const toastHost = document.createElement('div');
		document.body.appendChild(toastHost);
		const container = mount(ToastContainer, { target: toastHost });
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(progress()).toHaveLength(1);

		toastHost.querySelector<HTMLButtonElement>('.toast-close')!.click();
		flushSync();
		await vi.advanceTimersByTimeAsync(3_000);
		expect(progress()).toHaveLength(0);

		updateStatus.set(running('restarting'));
		flushSync();
		await vi.advanceTimersByTimeAsync(3_000);
		expect(progress()).toHaveLength(0);

		updateStatus.set(status({ available: false, operation: { operation_id: 'update-1', kind: 'update', status: 'complete', target_version: '1.1.0', settled_at: new Date().toISOString() } }));
		toasts.pin('update', { type: 'success', message: 'Updated to 1.1.0' });
		flushSync();
		await vi.advanceTimersByTimeAsync(1_000);
		expect(get(toasts).some((toast) => toast.message === 'Updated to 1.1.0')).toBe(true);

		unmount(container);
		toastHost.remove();
		for (const toast of get(toasts)) toasts.dismiss(toast.id);
	});

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

	it('tells assistive technology which step is current and which are done', () => {
		updateStatus.set(status({
			operation: {
				operation_id: 'update-1', kind: 'update', status: 'in_flight', phase: 'draining',
				drain_total: 2, blocking: 1, requested_at: new Date().toISOString(),
			},
		}));
		updateSurface.set('panel');
		component = mount(UpdateJourney, { target: host });
		flushSync();

		const steps = [...host.querySelectorAll('ol.steps li')];
		expect(steps.map((step) => step.getAttribute('aria-current'))).toEqual([null, 'step', null, null]);
		expect(steps[0].querySelector('.sr-only')?.textContent).toBe('Getting the update, done');
		expect(steps[1].querySelector('.sr-only')).toBeNull();
	});

	it('labels every available and additional change, then expands every returned item', () => {
		updateStatus.set(status({ changes: { kind: 'changelog', headline: null, items: ['One', 'Two', 'Three', 'Four', 'Five'], more: 2 } }));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		const disclosure = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Show all'))!;
		expect(disclosure.textContent).toContain('Show all 7 changes');
		expect(host.textContent).not.toContain('Four');
		disclosure.click();
		flushSync();
		expect(host.textContent).toContain('Four');
		expect(host.textContent).toContain('Five');
		expect(disclosure.textContent).toContain('Show fewer changes');
		expect(disclosure.querySelector('.chevron.expanded')).not.toBeNull();
	});

	it('keeps an unmapped failure reason and log detail behind one SVG disclosure', () => {
		updateStatus.set(status({ operation: { operation_id: 'update-1', status: 'failed', phase: 'draining', error: 'Raw failure reason.', failure_detail: '/tmp/update.log', conductor_told_at: '2026-10-13T00:00:00Z', log_tail: 'failure log' } }));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain('The update ended before CORSIA restarted.');
		expect(host.textContent).toContain('CORSIA kept running and nothing was changed.');
		expect(host.querySelector('.failure > .headline')?.textContent).not.toContain('Raw failure reason');
		const details = host.querySelector('details')!;
		expect(details.querySelectorAll('summary svg.chevron')).toHaveLength(1);
		expect(details.textContent).toContain('Raw failure reason.');
		expect(details.textContent).toContain('/tmp/update.log');
		expect(details.textContent).toContain('failure log');
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
		expect(host.textContent).toContain('Installed today at');
		expect(host.querySelector('p.version')?.textContent).toMatch(/^Installed today at/);
		expect(host.textContent).not.toContain('Version 1.1.0');
		expect(host.querySelector('.decision h3')).toBeNull();
		expect(host.textContent).toContain('Faster startup');
		expect(host.textContent).toContain('Done');
		expect(host.textContent).not.toContain('Update now');
	});

	it('renders no empty version line when the install time is unknown', () => {
		updateStatus.set(status({
			available: false,
			operation: { operation_id: 'update-1', kind: 'update', status: 'complete', target_version: '1.1.0', settled_at: null },
		}));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain("What's new in 1.1.0");
		expect(host.querySelector('p.version')).toBeNull();
	});

	it('shows active pre-drain agent count in the decision sheet', () => {
		updateStatus.set(status({ agents_working: 2 }));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain('2 agents are working');
	});

	it('words a single working agent in the singular', () => {
		updateStatus.set(status({ agents_working: 1 }));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain('1 agent is working: it gets up to');
		expect(host.textContent).not.toContain('1 agents');
	});

	it('promises a stopped agent a fresh start of its step, never a pickup where it left off', () => {
		updateStatus.set(status({ agents_working: 2, drain_cap_seconds: 60 }));
		updateSurface.set('sheet');
		component = mount(UpdateJourney, { target: host });
		flushSync();
		expect(host.textContent).toContain('they get up to a minute to reach a safe point');
		expect(host.textContent).toContain('start that step again once CORSIA is back');
		expect(host.textContent).not.toContain('left off');
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
