import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ProviderModelsBody from './ProviderModelsBody.svelte';
import { workspaceModels, type Model } from '$lib/stores/models';
import { groupModels } from '$lib/stores/modelGroups';
import { withAccountTurn } from '$lib/stores/modelFixtures';
import { token } from '$lib/stores/auth';
import { loadProviderModelsFor, noteModelsEvent } from '$lib/stores/providerModels';
import type { HostProvider } from '$lib/stores/providersV7';

vi.mock('$lib/stores/harnessProviders', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/harnessProviders')>()),
	harnessModels: async () => [
		{ id: 'claude-code/sonnet', dbId: 'claude-code/sonnet', name: 'Sonnet', state: 'loaded', meta: '', caps: [] },
	],
}));

const provider = {
	name: 'claude-code', label: 'Claude Code', type: 'harness', statusDot: 'online', subline: 'Signed in',
	raw: { name: 'claude-code', label: 'Claude Code', type: 'harness' },
} as HostProvider;

const catalogRow = (over: Partial<Model> & { status_reason?: string }): Model =>
	withAccountTurn({
		id: 'claude-code/sonnet', name: 'Sonnet', size: '', backend: 'network', speed: '',
		status: 'unavailable', status_reason: 'usage_limited', status_resets_at: '2026-10-09T07:23:34+00:00',
		...over,
	});

describe('Settings model rows and account usage', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		workspaceModels.set([]);
	});

	async function render(): Promise<string> {
		component = mount(ProviderModelsBody, { target: document.body, props: { provider } });
		flushSync();
		await tick();
		await tick();
		return document.body.textContent ?? '';
	}

	it('shows no limit on the model row when the selected account is usage-limited, while the picker group still does', async () => {
		const limited = catalogRow({});
		workspaceModels.set([limited]);

		const text = await render();

		expect(text).toContain('Sonnet');
		expect(text).not.toMatch(/usage limit|resets|checking account|verify account/i);
		expect(document.querySelector('[role="status"]')).toBeNull();
		expect(groupModels([limited])[0].state).toBe('limited');
	});

	it('keeps a model-level warning on the row', async () => {
		workspaceModels.set([catalogRow({ status_reason: 'connection_unavailable' })]);

		const text = await render();

		expect(text).toContain('this harness isn’t connected');
	});

	it('adds a recorded local download speed and note only to its matching server row', async () => {
		const localProvider = {
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'Local',
			raw: { name: 'omlx', label: 'oMLX', type: 'network' },
			server: { id: 'local-test', provider: 'omlx', kind: 'managed', source: 'manual', corsia_omlx: true, on_this_mac: true, instance_name: 'Local', endpoints: [] },
		} as unknown as HostProvider;
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.includes('/network-servers/local-test/models')) return {
				ok: true, json: async () => ({
					loaded: [],
					available: [
						{ id: 'exact-model', model_id: 'exact-model', repo_id: 'renamed-model', display_name: 'Measured model', context_length: 262144, size_bytes: 16 * 1024 ** 3 },
						{ id: 'plain', model_id: 'plain', repo_id: 'plain', display_name: 'Plain model', context_length: 262144, size_bytes: 16 * 1024 ** 3 },
					],
				}),
			};
			if (url.includes('/providers/local/downloads')) return {
				ok: true, json: async () => [
					{ repository: 'org/speedy', model: 'exact-model', status: 'available', server_id: 'local-test', speed: 52 },
				],
			};
			if (url.includes('/providers/sources')) return { ok: true, json: async () => ({ sources: [] }) };
			return { ok: true, json: async () => ({}) };
		}));

		component = mount(ProviderModelsBody, { target: document.body, props: { provider: localProvider, isLocal: true } });
		flushSync();
		await tick(); await tick(); await tick();

		expect(document.body.textContent).toContain('52 tokens/s');
		expect(document.body.textContent).toContain('Settings measured on Macs like this one · omlx.ai');
		expect(document.body.textContent).toContain('Plain model');
		expect(document.body.textContent?.match(/Settings measured on Macs like this one · omlx\.ai/g)).toHaveLength(1);
	});

	it('says why a downloaded model was not loaded, and that it is loading while CORSIA loads it', async () => {
		const localProvider = {
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'Local',
			raw: { name: 'omlx', label: 'oMLX', type: 'network' },
			server: { id: 'local-test', provider: 'omlx', kind: 'managed', source: 'manual', corsia_omlx: true, on_this_mac: true, instance_name: 'Local', endpoints: [] },
		} as unknown as HostProvider;
		token.set('test-token');
		let finishing = true;
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.includes('/network-servers/local-test/models')) return {
				ok: true, json: async () => ({
					loaded: [],
					available: [
						{ id: 'big-model', model_id: 'big-model', repo_id: 'big-model', display_name: 'Big model', context_length: 262144, size_bytes: 16 * 1024 ** 3, keep_line: 'Not loaded: needs 174 GB of memory, 123 GB free' },
						{ id: 'plain', model_id: 'plain', repo_id: 'plain', display_name: 'Plain model', context_length: 262144, size_bytes: 16 * 1024 ** 3 },
					],
				}),
			};
			if (url.includes('/providers/local/downloads')) return {
				ok: true, json: async () => [
					{ repository: 'org/big-model', model: 'big-model', status: 'available', server_id: 'local-test' },
				],
			};
			if (url.includes('/providers/sources')) return {
				ok: true, json: async () => ({ sources: finishing ? [{ source: 'omlx', settings_download: { name: 'Big model', repository: 'org/big-model', percent: null, failed: false, finishing: true } }] : [] }),
			};
			return { ok: true, json: async () => ({}) };
		}));

		component = mount(ProviderModelsBody, { target: document.body, props: { provider: localProvider, isLocal: true } });
		flushSync();
		await tick(); await tick(); await tick();

		// While CORSIA finishes: no "Downloading 0%" row, and the model's own row says it is loading.
		expect(document.body.textContent).not.toContain('Downloading');
		expect(document.body.textContent).toContain('Loading on this computer…');
		await unmount(component);
		finishing = false;
		component = mount(ProviderModelsBody, { target: document.body, props: { provider: localProvider, isLocal: true } });
		flushSync();
		await tick(); await tick(); await tick();

		expect(document.body.textContent).toContain('Not loaded: needs 174 GB of memory, 123 GB free');
		expect(document.body.textContent?.match(/Not loaded:/g)).toHaveLength(1);
	});

	it('offers download deletion only for an available oMLX model', async () => {
		const localProvider = {
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'Local',
			raw: { name: 'omlx', label: 'oMLX', type: 'network' },
			server: { id: 'local-test', provider: 'omlx', kind: 'managed', source: 'manual', corsia_omlx: true, on_this_mac: true, instance_name: 'Local', endpoints: [] },
		} as unknown as HostProvider;
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.includes('/network-servers/local-test/models')) return {
				ok: true, json: async () => ({
					loaded: [{ id: 'loaded-model', model_id: 'loaded-model', repo_id: 'loaded-model', display_name: 'Loaded model', context_length: 262144, size_bytes: 16 * 1024 ** 3 }],
					available: [{ id: 'available-model', model_id: 'available-model', repo_id: 'available-model', display_name: 'Available model', context_length: 262144, size_bytes: 16 * 1024 ** 3 }],
				}),
			};
			if (url.includes('/providers/local/downloads')) return { ok: true, json: async () => [] };
			if (url.includes('/providers/sources')) return { ok: true, json: async () => ({ sources: [] }) };
			return { ok: true, json: async () => ({}) };
		}));

		component = mount(ProviderModelsBody, { target: document.body, props: { provider: localProvider, isLocal: true } });
		flushSync();
		await tick(); await tick(); await tick();

		expect(document.querySelector('[role="group"][aria-label="Actions for Loaded model"]')).toBeNull();
		expect(document.querySelector('[role="group"][aria-label="Actions for Available model"]')).not.toBeNull();
	});

	it('shows a local engine\u2019s stored rows at once only while no model event has followed them', async () => {
		const localProvider = {
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'Local',
			raw: { name: 'omlx', label: 'oMLX', type: 'network' },
			server: { id: 'local-test', provider: 'omlx', kind: 'managed', source: 'manual', instance_name: 'Local', endpoints: [] },
		} as unknown as HostProvider;
		token.set('test-token');
		let answer = { loaded: [{ id: 'm1', model_id: 'm1', display_name: 'Held model' }], available: [] as unknown[] };
		const release: Array<() => void> = [];
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.includes('/network-servers/local-test/models')) {
				const body = answer;
				if (release.length === 0 && (globalThis as { hold?: boolean }).hold) await new Promise<void>((resolve) => release.push(resolve));
				return { ok: true, json: async () => body };
			}
			if (url.includes('/providers/sources')) return { ok: true, json: async () => ({ sources: [] }) };
			return { ok: true, json: async () => [] };
		}));
		await loadProviderModelsFor(localProvider.raw as never, localProvider.server);
		expect(document.body.textContent).not.toContain('Held model');

		(globalThis as { hold?: boolean }).hold = true;
		component = mount(ProviderModelsBody, { target: document.body, props: { provider: localProvider, isLocal: true } });
		flushSync();
		await tick();
		// Fresh: the stored rows render before this open's own read answers.
		expect(document.body.textContent).toContain('Held model');
		unmount(component); component = null; document.body.innerHTML = '';

		noteModelsEvent();
		release.length = 0;
		component = mount(ProviderModelsBody, { target: document.body, props: { provider: localProvider, isLocal: true } });
		flushSync();
		await tick();
		// Stale: a model event followed the stored read, so the rows wait for the open's own read.
		expect(document.body.textContent).not.toContain('Held model');
		answer = { loaded: [{ id: 'm1', model_id: 'm1', display_name: 'Held model' }], available: [] };
		release.forEach((resolve) => resolve());
		(globalThis as { hold?: boolean }).hold = false;
		await tick(); await tick(); await tick();
		expect(document.body.textContent).toContain('Held model');
	});
});
