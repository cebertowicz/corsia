import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const order: string[] = [];
vi.mock('$lib/stores/auth', async () => {
	const actual = await vi.importActual<typeof import('$lib/stores/auth')>('$lib/stores/auth');
	return { ...actual, logout: vi.fn(async () => { order.push('logout'); }) };
});

import SettingsSystem from './SettingsSystem.svelte';
import { connectOpen } from '$lib/stores/remoteConnect';
import { coreSettings } from '$lib/stores/settings';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let invoke: ReturnType<typeof vi.fn>;
const flat = (text: string | null | undefined) => (text ?? '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
const button = (label: string) => Array.from(host.querySelectorAll('button')).find((b) => flat(b.textContent) === label);
const devicesBody = { enabled: true, listening: { port: 47901 }, announced_name: 'Studio', local_hostname: 'studio.local', lan_addresses: ['192.168.1.20'], code: { state: 'none' } };

async function show() {
	component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
	flushSync();
	await settle();
}
// Every timer here (the countdown, the re-read) runs on the fake clock.
async function settle(ms = 0) { await tick(); await vi.advanceTimersByTimeAsync(ms); await tick(); }

beforeEach(() => {
	vi.useFakeTimers();
	order.length = 0;
	connectOpen.set(false);
	coreSettings.set({ summary_default_model: null, summary_default_model_name: null, summary_generation_status: 'active', summary_generation_message: null, workspace_context_files: [], features: {} } as never);
	invoke = vi.fn(async (command: string) => { order.push(command); });
	vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(devicesBody), { status: 200, headers: { 'Content-Type': 'application/json' } })));
	host = document.createElement('div'); document.body.appendChild(host);
});
afterEach(() => {
	if (component) void unmount(component);
	component = null; host.remove();
	delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
	coreSettings.set(null);
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('Settings › System with a shell', () => {
	it('on a computer connected to another, shows who it is connected to and Disconnect confirms first', async () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Alex’s Mac mini' };
		await show();
		expect(flat(host.querySelector('[data-testid="account-computer"]')?.textContent)).toContain('Connected to Alex’s Mac mini');
		button('Disconnect')?.click(); flushSync();
		expect(flat(host.textContent)).toContain('Disconnect from Alex’s Mac mini?');
		expect(flat(host.textContent)).toContain('Your work stays on Alex’s Mac mini. This app signs out and returns to setup.');
		expect(order).toEqual([]);
		button('Disconnect')?.click();
		await settle();
		expect(order).toEqual(['logout', 'disconnect_remote']);
	});
	it('Cancel leaves everything as it was', async () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio' };
		await show();
		button('Disconnect')?.click(); flushSync();
		button('Cancel')?.click(); flushSync();
		expect(flat(host.textContent)).not.toContain('Disconnect from Studio?');
		expect(order).toEqual([]);
	});
	it('seen from a connected computer, the Devices row is read-only and says where to manage it', async () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio' };
		await show();
		expect(flat(host.querySelector('[data-testid="devices-row"]')?.textContent)).toContain('Manage connections on Studio itself.');
		expect(host.querySelector('button[aria-label="Let other devices connect"]')).toBeNull();
	});
	it('on the computer itself, offers Connect to another computer, which opens the connect screens', async () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'local' };
		await show();
		expect(flat(host.querySelector('[data-testid="account-computer"]')?.textContent)).toContain('This computer');
		button('Connect to another computer')?.click();
		expect(get(connectOpen)).toBe(true);
		expect(button('Disconnect')).toBeUndefined();
	});
	it('in a browser or the phone app there is no shell, so no Computer row', async () => {
		await show();
		expect(host.querySelector('[data-testid="account-computer"]')).toBeNull();
		expect(flat(host.textContent)).not.toContain('Connect to another computer');
	});
});

describe('the Devices row countdown', () => {
	it('reads the host again when the code runs out', async () => {
		let reads = 0;
		vi.stubGlobal('fetch', vi.fn(async () => {
			reads += 1;
			const code = reads === 1 ? { state: 'active', value: 'K7QX', expires_in_seconds: 1 } : { state: 'none' };
			return new Response(JSON.stringify({ ...devicesBody, code }), { status: 200, headers: { 'Content-Type': 'application/json' } });
		}));
		await show();
		expect(flat(host.textContent)).toContain('Ends in under a minute');
		expect(reads).toBe(1);
		await settle(1500);
		expect(reads).toBeGreaterThanOrEqual(2);
		expect(flat(host.textContent)).not.toContain('Ends in');
	});
});
