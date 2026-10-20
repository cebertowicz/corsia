import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ProviderModelsEmpty from './ProviderModelsEmpty.svelte';
import { token } from '$lib/stores/auth';
import type { HostProvider } from '$lib/stores/providersV7';

describe('ProviderModelsEmpty', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		vi.unstubAllGlobals();
	});

	async function render(corsia_omlx: boolean): Promise<string> {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: true,
			json: async () => ({ sources: [{ source: 'omlx', eligible: true }] }),
		})));
		const provider = {
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'port 11434',
			raw: { name: 'omlx', label: 'oMLX' },
			server: { id: 'studio', instance_name: 'Studio', corsia_omlx },
		} as unknown as HostProvider;
		component = mount(ProviderModelsEmpty, { target: document.body, props: { provider, isExternal: false, isHarness: false } });
		flushSync();
		await tick(); await tick();
		return document.body.textContent ?? '';
	}

	it('names a person-owned oMLX server and tells the person where to add models', async () => {
		const text = await render(false);
		expect(text).toContain('No models on Studio. Add them in oMLX on that computer.');
		expect(document.querySelector('.phase-stub')).not.toBeNull();
	});

	it('keeps the local-machine empty copy for CORSIA’s writable oMLX', async () => {
		const text = await render(true);
		expect(text).toContain('No local models on this computer. Add one that fits.');
		expect(document.querySelector('.phase-stub')).not.toBeNull();
	});
});
