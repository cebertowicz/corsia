import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	providerModels,
	loadProviderModelsFor,
	pollJobsFor,
	markModelOptimistic,
	modelsForProvider,
} from '../providerModels';
import { loadModel, unloadModel } from '../providerModelCommands';
import { token } from '../auth';
import type { ProviderEntry } from '../providers';
import type { NetworkServer } from '../networkServers';

function provider(): ProviderEntry {
	return {
		name: 'omlx', label: 'omlx', type: 'network',
		has_key: false, masked_key: null, api_base: null, docs_url: null,
		models: [],
		};
	}
function server(): NetworkServer {
	return {
		id: 'srv-1', provider: 'omlx', instance_id: 'inst-1', instance_name: 'Mac',
		port: 5100, status: 'online', source: 'auto', latency_ms: 5,
		memory_total_mb: 32 * 1024, memory_used_mb: null, gpu_info: null,
		endpoints: [], active_host: null,
		api_base: 'http://127.0.0.1:5100', mgmt_base: 'http://127.0.0.1:5100',
		};
	}
interface FetchScript {
	when: (url: string) => boolean;
	respond: () => unknown;
	}
function installFetchScript(scripts: FetchScript[]) {
	const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		for (const s of scripts) {
			if (s.when(url)) {
				return new Response(JSON.stringify(s.respond()), {
					status: 200,
					headers: { 'Content-Type': 'application/json' },
				});
			}
		}
		throw new Error(`Unexpected fetch: ${url}`);
	});
	(globalThis as { fetch: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
	return fetchMock;
	}
function modelsView(p: ProviderEntry, srv?: NetworkServer) {
	return modelsForProvider(get(providerModels), p, srv);
}

describe('pollJobsFor — warm reconciliation', () => {
	beforeEach(() => {
		token.set('test-token');
		vi.restoreAllMocks();
	});

	it('flips a stuck cache_warming row back to loaded when the warm job disappears', async () => {
		const p = provider();
		const srv = server();

		const modelsResponse = {
			loaded: [{ model_id: 'qwen-3.6-35b', context_length: 131072, memory_gb: 19, db_id: 'db-qwen' }],
			available: [],
		};
		const seedFetch = installFetchScript([
			{ when: u => u.includes('/api/settings/network-servers/srv-1/models'), respond: () => modelsResponse },
		]);
		await loadProviderModelsFor(p, srv);
		expect(seedFetch).toHaveBeenCalled();
		expect(modelsView(p, srv).models[0]?.state).toBe('loaded');

		installFetchScript([
			{
				when: u => u.includes('/jobs/active'),
				respond: () => ({
					jobs: [{
						id: 'job-warm-1', target: 'qwen-3.6-35b', kind: 'warm', state: 'running',
						progress_current: 1024, progress_total: 4096, eta_seconds: 12,
					}],
				}),
			},
		]);
		await pollJobsFor(p, srv);
		const warming = modelsView(p, srv).models[0];
		expect(warming?.state).toBe('cache_warming');
		expect(warming?.jobId).toBe('job-warm-1');
		expect(warming?.meta).toMatch(/warming cache/);

		const completeFetch = installFetchScript([
			{ when: u => u.includes('/jobs/active'), respond: () => ({ jobs: [] }) },
			{ when: u => u.includes('/api/settings/network-servers/srv-1/models'), respond: () => modelsResponse },
		]);
		await pollJobsFor(p, srv);
		await Promise.resolve();
		await Promise.resolve();
		await new Promise(r => setTimeout(r, 0));

		const after = modelsView(p, srv).models[0];
		expect(after?.state).toBe('loaded');
		expect(after?.jobId).toBeUndefined();
		expect(after?.progress).toBeUndefined();
		const refetched = completeFetch.mock.calls.some(c => String(c[0]).includes('/api/settings/network-servers/srv-1/models'));
		expect(refetched).toBe(true);
	});

	it('reconciles cache_warming even when other jobs are active (multi-model case)', async () => {
		const p = provider();
		const srv = server();

		const modelsResponse = {
			loaded: [
				{ model_id: 'model-A', context_length: 8192, memory_gb: 4, db_id: 'db-A' },
				{ model_id: 'model-B', context_length: 8192, memory_gb: 4, db_id: 'db-B' },
			],
			available: [],
		};
		installFetchScript([
			{ when: u => u.includes('/api/settings/network-servers/srv-1/models'), respond: () => modelsResponse },
		]);
		await loadProviderModelsFor(p, srv);

		installFetchScript([
			{
				when: u => u.includes('/jobs/active'),
				respond: () => ({
					jobs: [
						{ id: 'jw-A', target: 'model-A', kind: 'warm', state: 'running', progress_current: 1, progress_total: 10 },
						{ id: 'jw-B', target: 'model-B', kind: 'warm', state: 'running', progress_current: 1, progress_total: 10 },
					],
				}),
			},
		]);
		await pollJobsFor(p, srv);
		expect(modelsView(p, srv).models.find(m => m.id === 'model-A')?.state).toBe('cache_warming');
		expect(modelsView(p, srv).models.find(m => m.id === 'model-B')?.state).toBe('cache_warming');

		installFetchScript([
			{
				when: u => u.includes('/jobs/active'),
				respond: () => ({
					jobs: [
						{ id: 'jw-B', target: 'model-B', kind: 'warm', state: 'running', progress_current: 5, progress_total: 10 },
					],
				}),
			},
			{ when: u => u.includes('/api/settings/network-servers/srv-1/models'), respond: () => modelsResponse },
		]);
		await pollJobsFor(p, srv);
		await new Promise(r => setTimeout(r, 0));

		expect(modelsView(p, srv).models.find(m => m.id === 'model-A')?.state).toBe('loaded');
		expect(modelsView(p, srv).models.find(m => m.id === 'model-B')?.state).toBe('cache_warming');
	});

	it('does NOT clobber an optimistic loading row when jobs/active is empty', async () => {
		const p = provider();
		const srv = server();

		const modelsResponse = {
			loaded: [],
			available: [{ repo_id: 'pending-model', context_length: 8192 }],
		};
		installFetchScript([
			{ when: u => u.includes('/api/settings/network-servers/srv-1/models'), respond: () => modelsResponse },
		]);
		await loadProviderModelsFor(p, srv);

		markModelOptimistic(p, srv, 'pending-model', 'loading');
		expect(modelsView(p, srv).models[0]?.state).toBe('loading');

		installFetchScript([
			{ when: u => u.includes('/jobs/active'), respond: () => ({ jobs: [] }) },
		]);
		await pollJobsFor(p, srv);

		expect(modelsView(p, srv).models[0]?.state).toBe('loading');
	});
});

describe('loadProviderModelsFor — alias + canonical dedup (#109)', () => {
	beforeEach(() => {
		token.set('test-token');
		vi.restoreAllMocks();
	});

	it('surfaces backend display_name as the row name, keeps canonical id', async () => {
		const p = provider();
		const srv = server();
		installFetchScript([
			{
				when: u => u.includes('/api/settings/network-servers/srv-1/models'),
				respond: () => ({
					loaded: [
						{
							id: 'Model-ABC-MLX-8bit',
							model_id: 'Model-ABC-MLX-8bit',
							display_name: 'MODEL ABC ALIAS',
							model_alias: 'MODEL ABC ALIAS',
							context_length: 128000,
							memory_gb: 200,
							db_id: 'db-deepseek',
						},
					],
					available: [
						{
							id: 'Model-XYZ-MLX-8bit',
							model_id: 'Model-XYZ-MLX-8bit',
							display_name: 'Model-XYZ-MLX-8bit',
							context_length: 256000,
							size_bytes: 36_000_000_000,
							db_id: 'db-qwen',
						},
					],
				}),
			},
		]);
		await loadProviderModelsFor(p, srv);
		const items = modelsView(p, srv).models;
		expect(items.length).toBe(2);
		const ds = items.find(m => m.id === 'Model-ABC-MLX-8bit');
		expect(ds).toBeDefined();
		expect(ds!.name).toBe('MODEL ABC ALIAS');
		expect(ds!.id).toBe('Model-ABC-MLX-8bit');
		const qw = items.find(m => m.id === 'Model-XYZ-MLX-8bit');
		expect(qw).toBeDefined();
		expect(qw!.name).toBe('Model-XYZ-MLX-8bit');
	});

	it('collapses alias + canonical rows to one entry per canonical id', async () => {
		const p = provider();
		const srv = server();
		installFetchScript([
			{
				when: u => u.includes('/api/settings/network-servers/srv-1/models'),
				respond: () => ({
					loaded: [
						{
							id: 'Model-ABC-MLX-8bit',
							model_id: 'Model-ABC-MLX-8bit',
							display_name: 'MODEL ABC ALIAS',
							context_length: 128000,
							db_id: 'db-deepseek',
						},
					],
					available: [
						{
							id: 'MODEL ABC ALIAS',
							model_id: 'MODEL ABC ALIAS',
							context_length: 128000,
						},
					],
				}),
			},
		]);
		await loadProviderModelsFor(p, srv);
		const items = modelsView(p, srv).models;
		const ids = items.map(m => m.id);
		expect(new Set(ids).size).toBe(ids.length);
		expect(ids).toContain('Model-ABC-MLX-8bit');
	});

	it('dedups same-id rows with loaded > default tiebreaker', async () => {
		const p = provider();
		const srv = server();
		installFetchScript([
			{
				when: u => u.includes('/api/settings/network-servers/srv-1/models'),
				respond: () => ({
					loaded: [
						{
							id: 'MyModel',
							model_id: 'MyModel',
							display_name: 'MyModel',
							context_length: 128000,
							db_id: 'db-mine',
						},
					],
					available: [
						{
							id: 'MyModel',
							model_id: 'MyModel',
							display_name: 'MyModel',
							context_length: 128000,
						},
					],
				}),
			},
		]);
		await loadProviderModelsFor(p, srv);
		const items = modelsView(p, srv).models;
		const matching = items.filter(m => m.id === 'MyModel');
		expect(matching.length).toBe(1);
		expect(matching[0].state).toBe('loaded');
		expect(matching[0].dbId).toBe('db-mine');
	});
});

describe('loadModel / unloadModel — External rows use the catalog update', () => {
	const external = (): NetworkServer => ({ ...server(), id: 'ns-tp', provider: 'external', kind: 'external' });
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn(async () => new Response('{}', { status: 200 }));
		vi.stubGlobal('fetch', fetchMock);
	});

	it('turns an External model on or off by PATCHing its catalog row', async () => {
		expect((await loadModel(provider(), external(), 'glm-5.3', 'ext-1')).ok).toBe(true);
		expect((await unloadModel(provider(), external(), 'glm-5.3', 'ext-1')).ok).toBe(true);
		const calls = fetchMock.mock.calls.map(([url, init]) => [String(url).split('?')[0], init?.method, init?.body]);
		expect(calls).toEqual([
			['/api/settings/models/ext-1', 'PATCH', JSON.stringify({ enabled: true })],
			['/api/settings/models/ext-1', 'PATCH', JSON.stringify({ enabled: false })],
		]);
	});

	it('refuses an External row with no catalog id without calling the server', async () => {
		const r = await loadModel(provider(), external(), 'glm-5.3', undefined);
		expect(r.ok).toBe(false);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('still loads a managed model through the server', async () => {
		await loadModel(provider(), server(), 'qwen', 'row-1');
		expect(String(fetchMock.mock.calls[0][0])).toContain('/api/settings/network-servers/srv-1/models/load');
	});
});
