import { afterEach, expect, it, vi } from 'vitest';
import { loadModel } from './providerModelCommands';
import type { ProviderEntry } from './providers';
import type { NetworkServer } from './networkServers';

afterEach(() => vi.unstubAllGlobals());

it.each(['', [{ msg: 'invalid' }]])('falls back for unusable error detail', async (detail) => {
 vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail }), { status: 422 })));
 const result = await loadModel({ name: 'omlx', type: 'network' } as ProviderEntry, { id: 'own' } as NetworkServer, 'model');
 expect(result).toEqual({ ok: false, message: 'Load failed' });
});
