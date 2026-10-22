import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { token } from '$lib/stores/auth';
import SettingsDevices from './SettingsDevices.svelte';
import type { DevicesApi } from '$lib/devices.svelte';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let responses: Response[];
let calls: { url: string; method: string; body?: string }[];

const api = (patch: Partial<DevicesApi> = {}): DevicesApi => ({
	enabled: true, listening: { port: 47901 }, announced_name: 'Studio', local_hostname: 'studio.local', computer_name: 'Studio Mac',
	lan_addresses: ['192.168.1.20'], code: { state: 'none' }, read_only: false, ...patch,
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const flat = (text: string | null | undefined) => (text ?? '').replace(/\s+/g, ' ').trim();

async function show() {
	component = mount(SettingsDevices, { target: host });
	await tick();
	await tick();
	flushSync();
}

async function settle() {
	await tick();
	await tick();
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('t-1');
	responses = [];
	calls = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		calls.push({ url: String(input), method: init?.method ?? 'GET', body: init?.body as string | undefined });
		return responses.shift() ?? json(api());
	}));
});

afterEach(() => {
	if (component) void unmount(component);
	component = null;
	host.remove();
	token.set(null);
	vi.unstubAllGlobals();
});

describe('SettingsDevices', () => {
	it('renders remote callers read-only from the server response while direct-loopback callers can manage devices', async () => {
		responses.push(json(api({ read_only: true })));
		await show();
		expect(flat(host.textContent)).toContain('Manage connections on Studio itself.');
		expect(host.querySelector('button[aria-label="Let other devices connect"]')).toBeNull();
		void unmount(component!);
		component = null;
		host.replaceChildren();

		responses.push(json(api({ read_only: false })));
		await show();
		expect(host.querySelector('button[aria-label="Let other devices connect"]')).not.toBeNull();
	});

	it('wires the existing outside-address field to save, refusal, and delete', async () => {
		responses.push(json(api()));
		await show();
		const input = host.querySelector<HTMLInputElement>('#outside-address');
		expect(input).not.toBeNull();
		input!.value = 'home.example.com';
		input!.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		responses.push(json(api({ outside_address: 'home.example.com' })));
		Array.from(host.querySelectorAll('button')).find((button) => flat(button.textContent) === 'Save')?.click();
		await settle();
		await vi.waitFor(() => expect(calls[1]).toMatchObject({ url: '/api/devices', method: 'PUT', body: JSON.stringify({ outside_address: 'home.example.com' }) }));
		await vi.waitFor(() => expect(Array.from(host.querySelectorAll('button')).find((button) => flat(button.textContent) === 'Delete')).toBeDefined());
		responses.push(json(api({ outside_address: null })));
		Array.from(host.querySelectorAll('button')).find((button) => flat(button.textContent) === 'Delete')?.click();
		await vi.waitFor(() => expect(calls[2]).toMatchObject({ url: '/api/devices', method: 'PUT', body: JSON.stringify({ outside_address: '' }) }));

		const refreshedInput = host.querySelector<HTMLInputElement>('#outside-address');
		refreshedInput!.value = 'invalid address';
		refreshedInput!.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		const refusal = 'Type a name, like home.example.com, or a name and port, like home.example.com:8443.';
		responses.push(json({ detail: refusal }, 400), json(api()));
		Array.from(host.querySelectorAll('button')).find((button) => flat(button.textContent) === 'Save')?.click();
		await vi.waitFor(() => expect(flat(host.textContent)).toContain(refusal));
	});
});
