import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { activeModelId, loadModels, models, workspaceModels } from './models';
import { readWorkspaceModels } from './modelsLoad';
import { activeThreadId } from './threads';
import { token } from './auth';

const originalFetch = globalThis.fetch;

afterEach(() => {
	globalThis.fetch = originalFetch;
	token.set(null);
	activeThreadId.set(null);
	activeModelId.set(null);
	models.set([]);
	workspaceModels.set([]);
	vi.restoreAllMocks();
});

describe('loadModels default selection', () => {
	it('does not invent a model for an open thread with no saved selection', async () => {
		activeThreadId.set('unbound-thread');
		activeModelId.set(null);
		globalThis.fetch = vi.fn(async () => ({
			json: async () => ({ models: [{ id: 'ready', litellm_model: 'mock/ready', backend: 'api', status: 'ready' }] }),
		} as Response)) as typeof fetch;

		await loadModels();

		expect(get(models)).toHaveLength(1);
		expect(get(activeModelId)).toBeNull();
	});

	it('keeps the last catalog when a response carries no rows', async () => {
		models.set([{ id: 'kept', litellm_model: 'mock/kept', backend: 'api', status: 'ready' } as never]);
		globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => ({}) } as Response)) as typeof fetch;

		await expect(loadModels()).resolves.toBeUndefined();

		expect(get(models)).toHaveLength(1);
		expect(get(workspaceModels)).toEqual([]);
	});
});

function query(path: string): Record<string, string> {
	const [pathname, search = ''] = path.split('?');
	return { pathname, ...Object.fromEntries(new URLSearchParams(search)) };
}

describe('loadModels account scope', () => {
	it("reads the open thread's account, so another account's limit does not grey its models out", async () => {
		token.set('session');
		activeThreadId.set('thread-on-second-account');
		const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) => ({
			ok: true,
			json: async () => ({ models: [{ id: 'codex/x', litellm_model: 'codex/x', backend: 'api', status: 'ready' }] }),
		} as Response));
		globalThis.fetch = fetchMock as unknown as typeof fetch;

		await loadModels({ refreshUsage: true });

		expect(query(fetchMock.mock.calls[0][0])).toEqual({
			pathname: '/api/models', refresh_usage: 'true', thread_id: 'thread-on-second-account',
		});

		expect(fetchMock.mock.calls[0][1]).toEqual({ headers: { Authorization: 'Bearer session' } });

		fetchMock.mockClear();
		activeThreadId.set('another-thread');
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
		expect(query(fetchMock.mock.calls[0][0]).thread_id).toBe('another-thread');
	});

	it('keeps the unscoped read with no open thread, signed in so the network listener accepts it', async () => {
		token.set('session');
		const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) => ({
			ok: true, json: async () => ({ models: [] }),
		} as Response));
		globalThis.fetch = fetchMock as unknown as typeof fetch;

		await loadModels();

		expect(fetchMock.mock.calls[0][0]).toBe('/api/models');
		expect(fetchMock.mock.calls[0][1]).toEqual({ headers: { Authorization: 'Bearer session' } });
	});

	it('signs the Settings workspace read too', async () => {
		token.set('session');
		const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) => ({
			ok: true, json: async () => ({ models: [] }),
		} as Response));
		globalThis.fetch = fetchMock as unknown as typeof fetch;

		await readWorkspaceModels();

		expect(fetchMock.mock.calls[0]).toEqual(['/api/models', { headers: { Authorization: 'Bearer session' } }]);
	});

	it('never shows one account status for another: a failed thread read leaves no row ready', async () => {
		token.set('session');
		activeThreadId.set('thread-a');
		let fail = false;
		const row = { id: 'codex/x', litellm_model: 'codex/x', backend: 'api', status: 'ready' };
		globalThis.fetch = vi.fn(async (path: string) => (fail && path.includes('thread_id')
			? { ok: false, status: 403, json: async () => ({}) }
			: { ok: true, json: async () => ({ models: [row] }) }) as Response) as unknown as typeof fetch;
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		await loadModels();
		expect(get(models)[0].status).toBe('ready');

		fail = true;
		await loadModels();
		expect(get(models).some((m) => m.status === 'ready')).toBe(false);
		expect(warn).toHaveBeenCalled();

		fail = false;
		await loadModels();
		expect(get(models)[0].status).toBe('ready');
	});

	it('keeps workspace surfaces on the unscoped catalog while a thread is open', async () => {
		token.set('session');
		activeThreadId.set('thread-on-second-account');
		globalThis.fetch = vi.fn(async (path: string) => ({
			ok: true,
			json: async () => ({ models: [{ id: 'codex/x', litellm_model: 'codex/x', backend: 'api', status: path.includes('thread_id') ? 'ready' : 'unavailable' }] }),
		} as Response)) as unknown as typeof fetch;

		await loadModels();

		expect(get(models)[0].status).toBe('ready');
		expect(get(workspaceModels)[0].status).toBe('unavailable');
	});
});
