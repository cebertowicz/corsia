import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';
import { networkServers } from '$lib/stores/networkServers';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));
vi.mock('$lib/utils/conductor', () => ({ fetchCorsiaAgent: vi.fn(async () => ({ kind: 'target', target: { thread_id: 'agent' } })) }));
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const settle = async () => { await tick(); await new Promise((done) => setTimeout(done)); await tick(); };
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | undefined;
afterEach(async () => { if (component) await unmount(component); component = undefined; host?.remove(); token.set(null); user.set(null); networkServers.set([]); vi.unstubAllGlobals(); });

it('shows the existing refusal and keeps it visible when another load is refused', async () => {
 const line = 'Not loaded: needs 12 GB, 8 GB free for a model now';
 const loads: string[] = [];
 let refused = false;
 token.set('test-session'); user.set({ id: 'user', name: 'Ada' });
 vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  if (url === '/api/setup/progress') return json({ setup_completed: false, status: { local_started: true, local_eligible: true, macos_supported: true } });
  if (url.includes('/sources')) return json({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'stopped' }] });
  if (url === '/api/providers/local/setup') return json({ state: 'start_failed', model: 'owner/local-model' });
  if (url.includes('/host-resources/check-load')) return json({ ok: false, free_gb: 8, after_load_gb: -4, safe_reserve_gb: 4, warning: 'Memory warning' });
  if (url.includes('/network-servers/own/models/load')) { loads.push(String(init?.body)); refused = true; return json({ detail: 'Retry failed' }, 500); }
  if (url.includes('/network-servers/own/models')) return json({ loaded: [], available: [{ id: 'local-model', size_bytes: 12 * 1024 ** 3, pinned: true, starting: false, keep_line: refused ? null : line }] });
  if (url.includes('/network-servers')) return json({ servers: [{ id: 'own', provider: 'omlx', corsia_omlx: true }] });
  if (url.includes('/settings/providers')) return json({ providers: [{ name: 'omlx', type: 'network' }] });
  if (url.startsWith('/api/models')) return json({ models: [] });
  return json({});
 }));
 host = document.createElement('div'); document.body.appendChild(host);
 component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn(), resume: true } });
 flushSync(); await settle(); await settle(); await settle();
 expect(host.textContent).toContain(line);
 const retry = [...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry');
 expect(retry).toBeTruthy(); retry!.click(); await settle(); await settle();
 expect(document.body.textContent).toContain('Memory warning');
 expect(loads).toEqual([]);
 [...document.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Load anyway')!.click();
 await settle(); await settle(); await settle();
 expect(loads).toEqual([JSON.stringify({ model: 'local-model' })]);
 expect(host.textContent).toContain(line);
 expect(host.textContent).toContain('Retry failed');
 expect([...host.querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Download')).toBe(false);
});
