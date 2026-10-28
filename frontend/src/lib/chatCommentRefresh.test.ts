import { afterEach, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';

const state = vi.hoisted(() => ({ handlers: new Map<string, (frame: any) => unknown>() }));
vi.mock('$lib/ws', () => ({ on: (name: string, handler: (frame: any) => unknown) => { state.handlers.set(name, handler); return () => state.handlers.delete(name); } }));
vi.mock('$lib/stores/auth', () => ({ token: writable('test-token') }));
vi.mock('$lib/stores/threads', () => ({ activeThreadId: writable('thread'), messages: writable(Array.from({ length: 5 }, (_, i) => ({ id: `m${i}`, thread_id: 'thread', role: 'assistant' }))) }));
vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload: vi.fn().mockResolvedValue(undefined) }));

import { watchChatCommentThreads } from './chatCommentRefresh';

afterEach(() => { vi.unstubAllGlobals(); state.handlers.clear(); });

it('refreshes a loaded transcript in page-sized queries', async () => {
  const batches: string[][] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    const ids = JSON.parse(String(options.body)).message_ids as string[];
    batches.push(ids);
    return { ok: true, json: async () => ({ messages: ids.map(id => ({ id })) }) };
  }));
  const stop = watchChatCommentThreads(2);
  await state.handlers.get('turn_complete')!({ thread_id: 'thread' });
  expect(batches).toEqual([['m0', 'm1'], ['m2', 'm3'], ['m4']]);
  stop();
});
