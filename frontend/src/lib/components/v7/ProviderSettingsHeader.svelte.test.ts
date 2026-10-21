import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import ProviderSettingsHeader from './ProviderSettingsHeader.svelte';
import type { HostProvider } from '$lib/stores/providersV7';
import type { ProviderEntry } from '$lib/stores/providers';

let component: ReturnType<typeof mount> | null = null;

const provider = (kind: 'managed' | 'external', on_this_mac: boolean, privateMachine = false): HostProvider => ({
	name: kind === 'managed' ? 'omlx' : 'external',
	label: kind === 'managed' ? 'oMLX' : 'OpenAI-compatible API',
	type: 'local',
	statusDot: 'online',
	subline: '127.0.0.1',
	raw: { name: kind === 'managed' ? 'omlx' : 'external', label: kind === 'managed' ? 'oMLX' : 'OpenAI-compatible API' } as ProviderEntry,
	server: {
		id: `${kind}-loopback`, provider: kind === 'managed' ? 'omlx' : 'external', instance_id: `${kind}-loopback`, instance_name: kind === 'managed' ? 'oMLX' : 'LM Studio', kind, port: kind === 'managed' ? 8000 : 1234, status: 'online', source: 'manual',
		latency_ms: null, memory_total_mb: null, memory_used_mb: null, gpu_info: null, endpoints: [], active_host: '127.0.0.1', api_base: null, mgmt_base: null,
		on_this_mac, private: privateMachine,
	},
});

function render(p: HostProvider) {
	component = mount(ProviderSettingsHeader, { target: document.body, props: { provider: p, tabs: [], activeTab: 'overview', onClose: () => {} } });
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
});

describe('ProviderSettingsHeader local tag', () => {
	it('shows LOCAL for a managed provider the backend says is on this computer', () => {
		render(provider('managed', true));
		expect(document.querySelector('.type-tag.local')?.textContent).toBe('local');
	});


	it('does not infer LOCAL from an unmarked External loopback', () => {
		render(provider('external', false));
		expect(document.querySelector('.type-tag')).toBeNull();
	});

	it('shows LOCAL for an External loopback marked private', () => {
		render(provider('external', false, true));
		expect(document.querySelector('.type-tag.local')?.textContent).toBe('local');
	});
});
