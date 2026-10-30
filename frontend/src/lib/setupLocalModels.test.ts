import { get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { token } from '$lib/stores/auth';
import { localLoadRefusal, refreshSetupLocalModels } from './setupLocalModels';

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { token.set(null); vi.unstubAllGlobals(); });

it('selects only the refused model named by the setup flight', async () => {
 let model: string | null = 'owner/setup-model';
 token.set('test-session');
 vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
  const url = String(input);
  if (url === '/api/providers/local/setup') return json({ model });
  if (url.includes('/network-servers/own/models')) return json({ loaded: [], available: [
   { id: 'other-model', pinned: true, keep_line: 'Other refusal' },
   { id: 'setup-model', pinned: true, keep_line: 'Setup refusal' },
  ] });
  if (url.includes('/network-servers')) return json({ servers: [{ id: 'own', provider: 'omlx', corsia_omlx: true }] });
  if (url.includes('/settings/providers')) return json({ providers: [{ name: 'omlx', type: 'network' }] });
  return json({});
 }));
 await refreshSetupLocalModels();
 expect(get(localLoadRefusal)?.model.id).toBe('setup-model');
 model = null;
 await refreshSetupLocalModels();
 expect(get(localLoadRefusal)).toBeUndefined();
});
