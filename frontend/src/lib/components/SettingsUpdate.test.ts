import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import SettingsUpdate from './SettingsUpdate.svelte';
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
	resetUpdateStoreForTests();
});

describe('SettingsUpdate', () => {
	it('keeps the current version while offering the target update', () => {
		updateStatus.set(status({ available: true, target_version: '1.1.0' }));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('1.0.0');
		expect(host.textContent).toContain('1.1.0 available');
		expect(host.textContent).toContain('Update');
	});

	it('shows the completed target as the last update', () => {
		updateStatus.set(status({
			operation: { operation_id: 'update-1', status: 'complete', target_version: '1.1.0' },
		}));
		component = mount(SettingsUpdate, { target: host });
		flushSync();

		expect(host.textContent).toContain('Last update');
		expect(host.textContent).toContain('Updated to 1.1.0');
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
