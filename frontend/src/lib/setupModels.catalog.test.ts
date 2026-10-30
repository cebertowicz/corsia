import { afterEach, describe, expect, it, vi } from 'vitest';

const fetchCorsiaAgent = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/conductor', () => ({ fetchCorsiaAgent }));

import { setupCatalog } from './setupModels';

afterEach(() => {
	vi.unstubAllGlobals(); vi.clearAllMocks();
});

describe('setup catalog read', () => {
	it('reads the catalog in the scope of the CORSIA agent', async () => {
		fetchCorsiaAgent.mockResolvedValue({ kind: 'target', target: { thread_id: 'first-chat' } });
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({ models: [{ id: 'codex/model', connection: 'codex', status: 'ready' }] }), { headers: { 'Content-Type': 'application/json' } }));
		vi.stubGlobal('fetch', fetchMock);

		await expect(setupCatalog('disposable-session')).resolves.toMatchObject([{ id: 'codex/model' }]);
		expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toBe('/api/models?thread_id=first-chat');
	});

	it.each([422, 503])('fails when the catalog answers HTTP %i', async (status) => {
		fetchCorsiaAgent.mockResolvedValue({ kind: 'target', target: { thread_id: 'first-chat' } });
		vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status })));
		await expect(setupCatalog('disposable-session')).rejects.toThrow();
	});

	it('fails when the CORSIA agent cannot be found', async () => {
		fetchCorsiaAgent.mockResolvedValueOnce({ kind: 'not_found' });
		await expect(setupCatalog('disposable-session')).rejects.toThrow();
	});
});
