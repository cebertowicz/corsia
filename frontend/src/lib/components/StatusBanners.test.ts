import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import StatusBanners from './StatusBanners.svelte';
import {
	connectionState,
	drainStatus,
	securityAlert,
} from '$lib/stores/connection';
import { token } from '$lib/stores/auth';
import { resetUpdateStoreForTests, updateStatus } from '$lib/update/store';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	connectionState.set('connected');
	drainStatus.set(null);
	securityAlert.set(null);
	token.set(null);
	resetUpdateStoreForTests();
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
	resetUpdateStoreForTests();
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

	it('suppresses the generic banner only while an update owns the drain', () => {
		drainStatus.set({
			active: true, reason: 'update', activeProcesses: 1, activeTurns: 0,
			hostAccessSupervisors: 0, ownedProcessGroups: 0, browserCommands: 0,
			turnAdmissions: 0, countdown: null, blockingTurns: [],
		});
		updateStatus.set({
			branch: 'dev', current_sha: 'a', remote_sha: 'b', commits_behind: 1,
			available: true, checked_at: 1, error: null,
			operation: { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase: 'draining' },
		});
		component = mount(StatusBanners, { target: host });
		flushSync();
		expect(host.querySelector('.drain-banner')).toBeNull();

		updateStatus.update((status) => ({
			...status!, operation: { operation_id: 'restart-1', kind: 'restart', status: 'in_flight', phase: 'draining' },
		}));
		flushSync();
		expect(host.querySelector('.drain-banner')).not.toBeNull();
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

	// Chat content scrolls under this strip, so a banner painted with a translucent token alone lets the lines
	// behind it show through the text. jsdom cannot paint, so read the rule from the component's own style.
	it.each(['drain-banner', 'security-banner', 'connection-banner'])(
		'paints %s over an opaque surface',
		(name) => {
			const source = readFileSync(resolve(__dirname, 'StatusBanners.svelte'), 'utf8');
			const rule = source.match(new RegExp(`\\.${name} \\{([^}]*)\\}`))?.[1] ?? '';
			const background = rule.match(/background:\s*([^;]+);/)?.[1] ?? '';
			const opaque = /^(#[0-9a-f]{6}|var\(--yellow\))$/i.test(background.trim())
				|| background.includes('var(--bg-primary)');
			expect(opaque, `${name} background: ${background}`).toBe(true);
		},
	);

	it('keeps every banner inside the strip that sits below the header', () => {
		connectionState.set('error');
		securityAlert.set({ date: '2026-10-14', vulns: 1, errors: 0, report: 'report.md' });
		component = mount(StatusBanners, { target: host });
		flushSync();

		const strip = host.querySelector('.status-banners');
		expect(strip).not.toBeNull();
		expect(strip!.querySelectorAll('.security-banner, .connection-banner')).toHaveLength(2);
	});
});
