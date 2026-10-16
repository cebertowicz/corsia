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
	await tick(); await new Promise((done) => setTimeout(done)); await tick();
}

beforeEach(() => {
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
	vi.unstubAllGlobals();
});

describe('Settings › System with a shell', () => {
	it('on a computer connected to another, shows who it is connected to and Disconnect confirms first', async () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Steve’s Mac Studio' };
		await show();
		expect(flat(host.querySelector('[data-testid="account-computer"]')?.textContent)).toContain('Connected to Steve’s Mac Studio');
		button('Disconnect')?.click(); flushSync();
		expect(flat(host.textContent)).toContain('Disconnect from Steve’s Mac Studio?');
		expect(flat(host.textContent)).toContain('Your work stays on Steve’s Mac Studio. This app signs out and returns to setup.');
		expect(order).toEqual([]);
		button('Disconnect')?.click();
		await vi.waitFor(() => expect(order).toEqual(['logout', 'disconnect_remote']));
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
