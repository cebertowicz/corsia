import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import StatusBanners from './StatusBanners.svelte';
import {
	connectionState,
	drainStatus,
	securityAlert,
} from '$lib/stores/connection';
import { token } from '$lib/stores/auth';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	connectionState.set('connected');
	drainStatus.set(null);
	securityAlert.set(null);
	token.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	connectionState.set('disconnected');
	drainStatus.set(null);
	securityAlert.set(null);
	token.set(null);
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('StatusBanners', () => {
	it('names the active thread blocking restart', () => {
		drainStatus.set({
			active: true,
			reason: 'update',
			activeProcesses: 5,
			activeTurns: 2,
			hostAccessSupervisors: 0,
			ownedProcessGroups: 1,
			browserCommands: 2,
			turnAdmissions: 0,
			countdown: null,
			blockingTurns: [{
				threadId: 'thread-815f581e',
				turnId: 'turn-1',
				turnOrder: 4,
				executionClass: 'native',
				title: 'Inspect Batch 2 history/tool storage',
				startedAt: '2026-08-03T21:47:32Z',
			}, {
				threadId: 'thread-other',
				turnId: 'turn-2',
				turnOrder: 5,
				executionClass: 'native',
				title: 'Other turn',
				startedAt: '2026-08-03T21:48:32Z',
			}],
		});

		component = mount(StatusBanners, { target: host });
		flushSync();

		expect(host.textContent).toContain(
			'waiting on “Inspect Batch 2 history/tool storage” (thread-815f581e)',
		);
		expect(host.textContent).toContain('1 more active turn');
		expect(host.textContent).toContain('1 owned process group');
		expect(host.textContent).toContain('2 browser commands');
	});

	it('refreshes the exact drain blocker while restart is pending', async () => {
		vi.useFakeTimers();
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({
				active: true,
				reason: 'update',
				activeProcesses: 1,
				activeTurns: 1,
				hostAccessSupervisors: 0,
				ownedProcessGroups: 0,
				browserCommands: 0,
				turnAdmissions: 0,
				countdown: null,
				blockingTurns: [{
					threadId: 'thread-new',
					turnId: 'turn-new',
					turnOrder: 6,
					executionClass: 'native',
					title: 'Current blocker',
					startedAt: '2026-08-03T21:49:32Z',
				}],
			}),
		}));
		drainStatus.set({
			active: true,
			reason: 'update',
			activeProcesses: 1,
			activeTurns: 1,
			hostAccessSupervisors: 0,
			ownedProcessGroups: 0,
			browserCommands: 0,
			turnAdmissions: 0,
			countdown: null,
			blockingTurns: [{
				threadId: 'thread-old',
				turnId: 'turn-old',
				turnOrder: 4,
				executionClass: 'native',
				title: 'Old blocker',
				startedAt: '2026-08-03T21:47:32Z',
			}],
		});

		component = mount(StatusBanners, { target: host });
		await vi.advanceTimersByTimeAsync(1000);
		flushSync();

		expect(fetch).toHaveBeenCalledWith(
			'/api/system/drain?token=test-token',
			{ cache: 'no-store' },
		);
		expect(host.textContent).toContain(
			'waiting on “Current blocker” (thread-new)',
		);
	});

	it('renders the authoritative security audit report path', () => {
		const report =
			'/Users/alice/Library/Application Support/com.cebertowicz.corsia/state/security/audit-2026-07-22.md';
		securityAlert.set({
			vulns: 2,
			errors: 0,
			report,
			date: '2026-07-22',
		});

		component = mount(StatusBanners, { target: host });
		flushSync();

		expect(host.querySelector('.security-banner code')?.textContent).toBe(report);
		expect(host.textContent).not.toContain('workspace/security');
	});
});
