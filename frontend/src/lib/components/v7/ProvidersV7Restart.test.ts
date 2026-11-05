import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ProvidersV7View from './ProvidersV7View.svelte';
import { groupByHost, hostGroupSnapshot } from '$lib/stores/providersV7';
import { networkServers, type NetworkServer } from '$lib/stores/networkServers';

const server: NetworkServer = {
	id: 'restart-fixture', provider: 'omlx', instance_id: 'fixture', instance_name: 'CORSIA', kind: 'managed',
	port: 8210, status: 'online', source: 'manual', latency_ms: null,
	memory_total_mb: null, memory_used_mb: null, gpu_info: null, corsia_omlx: true,
	endpoints: [{ id: 'endpoint', server_id: 'restart-fixture', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }],
	active_host: '127.0.0.1', api_base: null, mgmt_base: null,
};
let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	hostGroupSnapshot.set([]);
	networkServers.set([]);
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

it('shows the live restart through unavailable health, then the real success or stopped state', () => {
	vi.stubGlobal('fetch', vi.fn(() => new Promise<never>(() => {})));
	networkServers.set([server]);
	hostGroupSnapshot.set(groupByHost([], [server], null));
	component = mount(ProvidersV7View, { target: document.body });
	flushSync();
	const row = () => document.querySelector('[data-server-card="restart-fixture"]')!;
	expect(row().querySelector('.meta')?.textContent).toBe('port 8210'); // waiting for quiet

	for (const status of ['online', 'offline'] as const) {
		networkServers.set([{ ...server, status, restarting: true }]);
		flushSync();
		expect(row().querySelector('.meta')?.textContent).toBe('Restarting…');
		expect(row().querySelector('.dot.idle')).not.toBeNull();
		expect(row().querySelector('button[aria-label="Settings"]')).not.toBeNull();
	}
	networkServers.set([{ ...server, restarting: false }]);
	flushSync();
	expect(row().querySelector('.meta')?.textContent).toBe('port 8210'); // reload completed
	expect(row().querySelector('.dot.online')).not.toBeNull();

	networkServers.set([{ ...server, status: 'offline', restarting: false }]);
	flushSync();
	expect(row().querySelector('.meta')?.textContent).toBe('off · port 8210'); // failed boot or Stop
	expect(row().textContent).not.toContain('Restarting');
});
