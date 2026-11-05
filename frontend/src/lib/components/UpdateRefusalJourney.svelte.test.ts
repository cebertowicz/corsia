import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import UpdateScreen from './UpdateScreen.svelte';
import SettingsUpdate from './SettingsUpdate.svelte';
import { startUpdateScreen, stopUpdateScreen, tickUpdateScreen } from '$lib/update/screenStore';
import { resetUpdateStoreForTests, updateStatus, type UpdateStatus } from '$lib/update/store';

let components: Record<string, unknown>[] = [];
afterEach(() => {
	components.forEach((component) => unmount(component));
	components = [];
	stopUpdateScreen();
	resetUpdateStoreForTests();
	vi.unstubAllGlobals();
	document.body.innerHTML = '';
});

it('dismisses a refusal and shows its reason and own log in Settings Details', () => {
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })));
	startUpdateScreen();
	const operation = { operation_id: 'a'.repeat(32), kind: 'update', status: 'in_flight', phase: 'draining' } as const;
	updateStatus.set({ operation } as UpdateStatus);
	tickUpdateScreen();
	updateStatus.set({ operation: {
		...operation, status: 'failed', phase: null, failure_code: 'update_refused',
		error: 'Prepared build is missing', failure_detail: JSON.stringify({ backend_healthy: true, log_path: '/logs/update-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.log' }),
		log_tail: 'Update refused: Prepared build is missing',
	} } as UpdateStatus);
	tickUpdateScreen();
	components.push(mount(UpdateScreen, { target: document.body }));
	flushSync();
	expect(document.querySelector('#update-screen-title')?.textContent).toBe("Update didn't finish");
	expect(document.body.textContent).toContain('CORSIA is still running the version it had.');
	expect([...document.querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Retry')).toBe(true);
	const dismiss = [...document.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Dismiss');
	expect(dismiss).toBeDefined();
	dismiss!.click();
	flushSync();
	components.push(mount(SettingsUpdate, { target: document.body }));
	flushSync();
	const details = document.querySelector('details');
	expect(details?.querySelector('summary')?.textContent).toBe('Details');
	expect(details?.textContent).toContain('Prepared build is missing');
	expect(details?.textContent).toContain('update-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.log');
	expect(details?.textContent).not.toContain('restart.log');
});
