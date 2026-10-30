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

	async function render(corsia_omlx: boolean, on_this_mac = false, status = 'online', row: Record<string, unknown> = { eligible: true }): Promise<string> {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: true,
			json: async () => ({ sources: [{ source: 'omlx', ...row }] }),
		})));
		const provider = {
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'port 11434',
			raw: { name: 'omlx', label: 'oMLX' },
			server: { id: 'studio', instance_name: 'Studio', corsia_omlx, on_this_mac, status },
		} as unknown as HostProvider;
		component = mount(ProviderModelsEmpty, { target: document.body, props: { provider, isExternal: false, isHarness: false } });
		flushSync();
		await tick(); await tick();
		return document.body.textContent ?? '';
	}

	it('names a person-owned oMLX server on another computer and tells the person where to add models', async () => {
		const text = await render(false);
		expect(text).toContain('No models on Studio. Add them in oMLX on that computer.');
		expect(document.querySelector('.phase-stub')).not.toBeNull();
	});

	it('still names a remote oMLX with no models on a host that can never run a local model', async () => {
		const text = await render(false, false, 'online', { eligible: false, never_local: true });
		expect(text).toContain('No models on Studio. Add them in oMLX on that computer.');
	});

	it('keeps a low-memory Mac quiet: its refusal is shown elsewhere', async () => {
		const text = await render(false, false, 'online', { eligible: false });
		expect(text).not.toContain('No models on Studio');
	});

	it('sends a person to their own oMLX on this computer without pointing at another computer', async () => {
		const text = await render(false, true);
		expect(text).toContain('No models on Studio. Add them in oMLX.');
		expect(text).not.toContain('that computer');
	});

	it('keeps the local-machine empty copy for CORSIA’s own oMLX, online or offline', async () => {
		for (const status of ['online', 'offline']) {
			const text = await render(true, true, status);
			expect(text).toContain('No local models on this computer. Add one that fits.');
			unmount(component!); component = null; document.body.innerHTML = '';
		}
	});
});
