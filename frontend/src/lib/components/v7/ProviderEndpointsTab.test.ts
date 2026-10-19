import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ProviderEndpointsTab from './ProviderEndpointsTab.svelte';
import { token } from '$lib/stores/auth';
import type { NetworkServer } from '$lib/stores/networkServers';

const DESCRIPTION = 'Its models count as local for local-only chats and cloud-off projects. Only turn this on if its models run on that machine, not on a service it forwards to.';
const THIS_MAC = 'On this Mac: its models count as local.';
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
const toggle = () => host.querySelector('[aria-label="Private machine"]') as HTMLButtonElement | null;
function render(s: NetworkServer, put: 'ok' | 'fail' = 'ok') {
	vi.stubGlobal('fetch', vi.fn(async (_i: RequestInfo | URL, init?: RequestInit) => {
		bodies.push(JSON.parse(init?.body as string));
		return put === 'ok' ? { ok: true, json: async () => ({}) } : { ok: false, status: 500, json: async () => ({ detail: 'Server unavailable' }) };
	}));
	component = mount(ProviderEndpointsTab, { target: host, props: { server: s } });
	flushSync();
}

beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); token.set('tok'); bodies = []; });
afterEach(() => { if (component) unmount(component); component = null; host.remove(); token.set(null); vi.unstubAllGlobals(); });

describe('ProviderEndpointsTab private machine', () => {
	it('another machine, off: switch and description', () => {
		render(server({}));
		expect(text()).toContain('Private machine'); expect(text()).toContain(DESCRIPTION);
		expect(toggle()).not.toBeNull(); expect(text()).not.toContain(THIS_MAC);
	});
	it('on: the saved mark shows as a set switch', () => {
		render(server({ private: true }));
		expect(toggle()?.getAttribute('aria-pressed')).toBe('true'); expect(text()).toContain(DESCRIPTION);
	});
	it('managed oMLX on this Mac: the line, no switch', () => {
		render(server({ endpoints: [endpoint('127.0.0.1', 'local')] }));
		expect(text()).toContain(THIS_MAC); expect(toggle()).toBeNull(); expect(text()).not.toContain('Private machine');
	});
	it('External on loopback: the switch, address alone does not count', () => {
		render(server({ provider: 'external', kind: 'external', endpoints: [endpoint('127.0.0.1', 'local')] }));
		expect(toggle()).not.toBeNull(); expect(text()).not.toContain(THIS_MAC);
	});
	it('offline server: the switch still works', async () => {
		render(server({ status: 'offline' }));
		toggle()!.click(); flushSync();
		await vi.waitFor(() => expect(bodies).toEqual([{ private: true }]));
		await vi.waitFor(() => expect(toggle()?.getAttribute('aria-pressed')).toBe('true'));
	});
	it('save failed: names the label, direction and error, and the switch stays off', async () => {
		render(server({}), 'fail');
		toggle()!.click(); flushSync();
		await vi.waitFor(() => expect(text()).toContain('Could not turn Private machine on: Server unavailable'));
		expect(toggle()?.getAttribute('aria-pressed')).toBe('false');
	});
});
