import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { activeModelId, models, workspaceModels } from './models';
import { loadComposerModels, loadThreadCatalog } from './modelsLoad';
import { threadCatalogs } from './modelsState';
import { activeThreadId } from './threads';
import { token } from './auth';

const originalFetch = globalThis.fetch;
const row = (id: string) => ({ id, litellm_model: id, backend: 'api', status: 'ready' });

function query(path: string): Record<string, string> {
	const [pathname, search = ''] = path.split('?');
	return { pathname, ...Object.fromEntries(new URLSearchParams(search)) };
}

afterEach(() => {
	globalThis.fetch = originalFetch;
	token.set(null);
	activeThreadId.set(null);
	activeModelId.set(null);
	models.set([]);
	workspaceModels.set([]);
	threadCatalogs.set(new Map());
	vi.restoreAllMocks();
});

describe('loadThreadCatalog', () => {
	it("reads another thread's account into its own entry and leaves the open thread's catalog alone", async () => {
		token.set('session');
		activeThreadId.set('open');
		models.set([row('open-model') as never]);
		const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) => ({
			ok: true, json: async () => ({ models: [row('worker-model')] }),
		} as Response));
		globalThis.fetch = fetchMock as unknown as typeof fetch;

		await loadComposerModels('worker');

		expect(query(fetchMock.mock.calls[0][0])).toEqual({ pathname: '/api/models', thread_id: 'worker' });
		expect(get(threadCatalogs).get('worker')?.map((m) => m.id)).toEqual(['worker-model']);
		expect(get(models).map((m) => m.id)).toEqual(['open-model']);
	});

	it("reads the open thread's catalog for the open thread's composer", async () => {
		token.set('session');
		activeThreadId.set('open');
		globalThis.fetch = vi.fn(async () => ({
			ok: true, json: async () => ({ models: [row('open-model')] }),
		} as Response)) as unknown as typeof fetch;

		await loadComposerModels('open');

		expect(get(models).map((m) => m.id)).toEqual(['open-model']);
		expect(get(threadCatalogs).size).toBe(0);
	});

	it("marks every status unknown when the read fails, never another thread's", async () => {
		token.set('session');
		workspaceModels.set([row('worker-model') as never]);
		vi.spyOn(console, 'warn').mockImplementation(() => {});
		globalThis.fetch = vi.fn(async () => ({ ok: false, status: 500 } as Response)) as unknown as typeof fetch;

		await loadThreadCatalog('worker');

		expect(get(threadCatalogs).get('worker')).toEqual([
			expect.objectContaining({ id: 'worker-model', status: 'unavailable', status_reason: 'status_unknown' }),
		]);
	});

	it('keeps only the newest read when two overlap', async () => {
		token.set('session');
		const slow = Promise.withResolvers<Response>();
		globalThis.fetch = vi.fn()
			.mockReturnValueOnce(slow.promise)
			.mockResolvedValueOnce({ ok: true, json: async () => ({ models: [row('newer')] }) } as Response) as unknown as typeof fetch;

		const first = loadThreadCatalog('worker');
		await loadThreadCatalog('worker');
		slow.resolve({ ok: true, json: async () => ({ models: [row('older')] }) } as Response);
		await first;

		expect(get(threadCatalogs).get('worker')?.map((m) => m.id)).toEqual(['newer']);
	});
});
