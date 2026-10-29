import { afterEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { allThreads, coordinationThreads } from './threads';
import { token } from './auth';
import { togglePin } from './pinnedConversations';
import { setWs } from '../ws/state';
import { sendMessage } from '../ws/send';
import { updateThreadRow } from './threadRow';
const row = { id: 'owner', user_id: 'u', profile: 'personal', title: 'Owner', model: null, coordination: 'conductor' as const, created_at: '2026-01-01', updated_at: '2026-01-01' };
afterEach(() => { setWs(null); token.set(null); allThreads.set([]); vi.unstubAllGlobals(); });
it('send updates the shared row', () => {
  allThreads.set([row]);
  setWs({readyState: WebSocket.OPEN, send: vi.fn()} as unknown as WebSocket);
  sendMessage('owner', 'hello');
  expect(get(coordinationThreads)[0].last_message).toBe('hello');
});
it('pin updates the shared row', async () => {
  token.set('session'); allThreads.set([row]);
  vi.stubGlobal('fetch', vi.fn(async () => ({ok: true})));
  await togglePin('owner');
  expect(get(coordinationThreads)[0].pinned).toBe(1);
});
it('ordinary edit writer updates the shared row', () => {
  allThreads.set([row]);
  updateThreadRow('owner', {reasoning_effort: 'high'});
  expect(get(coordinationThreads)[0].reasoning_effort).toBe('high');
});
