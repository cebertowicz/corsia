import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ProviderEndpointsTab from './ProviderEndpointsTab.svelte';
import { token } from '$lib/stores/auth';
import type { NetworkServer } from '$lib/stores/networkServers';

const DESCRIPTION = 'Its models count as local, so local-only chats and projects can use them. Turn this on only if this is your own secure, trusted device and the models run on it, not on a service it forwards to.';
const THIS_COMPUTER = 'This computer';
const endpoint = (host: string, kind: 'local' | 'lan') => ({ id: `e-${host}`, server_id: 's1', host, kind, last_ok_at: null, last_latency_ms: null });
const server = (over: Partial<NetworkServer>): NetworkServer => ({
	id: 's1', provider: 'omlx', instance_id: 'i1', instance_name: 'Studio', kind: 'managed', port: 8000, status: 'online', source: 'auto',
	latency_ms: null, memory_total_mb: null, memory_used_mb: null, gpu_info: null, active_host: null, api_base: null, mgmt_base: null,
	endpoints: [endpoint('192.168.1.20', 'lan')], ...over,
});

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let bodies: unknown[] = [];
const text = () => (host.textContent ?? '').replace(/\s+/g, ' ');
const toggle = () => host.querySelector('[aria-label="Trusted device"]') as HTMLButtonElement | null;
function render(s: NetworkServer, put: 'ok' | 'fail' = 'ok') {
	vi.stubGlobal('fetch', vi.fn(async (_i: RequestInfo | URL, init?: RequestInit) => {
		bodies.push(JSON.parse(init?.body as string));
		return put === 'ok' ? { ok: true, json: async () => ({}) } : { ok: false, status: 500, json: async () => ({ detail: 'Your permission changed' }) };
	}));
	component = mount(ProviderEndpointsTab, { target: host, props: { server: s } });
	flushSync();
}

beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); token.set('tok'); bodies = []; });
afterEach(() => { if (component) unmount(component); component = null; host.remove(); token.set(null); vi.unstubAllGlobals(); });

describe('ProviderEndpointsTab trusted device', () => {
	it('another machine, off: switch and description', () => {
		render(server({}));
		expect(text()).toContain('Trusted device'); expect(text()).toContain(DESCRIPTION);
		expect(toggle()).not.toBeNull(); expect(text()).not.toContain(THIS_COMPUTER);
	});
	it('on: the saved mark shows as a set switch', () => {
		render(server({ private: true }));
		expect(toggle()?.getAttribute('aria-pressed')).toBe('true'); expect(text()).toContain(DESCRIPTION);
	});
	it('managed oMLX on this computer: the caption, no switch', () => {
		render(server({ endpoints: [endpoint('127.0.0.1', 'local')], on_this_mac: true }));
		expect(text()).toContain(THIS_COMPUTER); expect(toggle()).toBeNull(); expect(text()).not.toContain('Trusted device');
		expect(host.querySelector('.ep-kind.local')).toBeNull();
	});
	it('External on loopback: the address caption and the switch', () => {
		render(server({ provider: 'external', kind: 'external', endpoints: [endpoint('127.0.0.1', 'local')], on_this_mac: false }));
		expect(toggle()).not.toBeNull(); expect(text()).toContain(THIS_COMPUTER); expect(host.querySelector('.ep-kind.local')).toBeNull();
	});
	it('offline server: the switch still works', async () => {
		render(server({ status: 'offline' }));
		toggle()!.click(); flushSync();
		await vi.waitFor(() => expect(bodies).toEqual([{ private: true }]));
		await vi.waitFor(() => expect(toggle()?.getAttribute('aria-pressed')).toBe('true'));
	});
	it('save failed: uses the shared retry wording and the switch stays off', async () => {
		render(server({}), 'fail');
		toggle()!.click(); flushSync();
		await vi.waitFor(() => expect(text()).toContain("Couldn't save this change. Your permission changed. Try again."));
		expect(host.querySelector('.status.error')).not.toBeNull();
		expect(toggle()?.getAttribute('aria-pressed')).toBe('false');
	});
	it('save failed turning it off: the same line, and the switch stays on', async () => {
		render(server({ private: true }), 'fail');
		toggle()!.click(); flushSync();
		await vi.waitFor(() => expect(text()).toContain("Couldn't save this change. Your permission changed. Try again."));
		expect(toggle()?.getAttribute('aria-pressed')).toBe('true');
	});
});
