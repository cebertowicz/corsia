import { afterEach, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';

const state = vi.hoisted(() => ({ handlers: new Map<string, (frame: any) => unknown>() }));
vi.mock('$lib/ws', () => ({ on: (name: string, handler: (frame: any) => unknown) => { state.handlers.set(name, handler); return () => state.handlers.delete(name); } }));
vi.mock('$lib/stores/auth', () => ({ token: writable('test-token') }));
vi.mock('$lib/stores/threads', () => ({ activeThreadId: writable('thread'), messages: writable([]) }));
vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload: vi.fn().mockResolvedValue(undefined) }));

import { watchChatCommentThreads } from './chatCommentRefresh';
import { messages } from '$lib/stores/threads';

const row = (id: string, seq: number, extra: object = {}) => ({ id, thread_id: 'thread', role: 'assistant' as const, content: '', created_at: 't', seq, ...extra });
afterEach(() => { vi.unstubAllGlobals(); state.handlers.clear(); messages.set([]); });

it('does nothing for a thread with no comments', async () => {
  messages.set([row('one', 1)]);
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
  const stop = watchChatCommentThreads(50);
  await state.handlers.get('turn_complete')!({ thread_id: 'thread', step_ids: ['one'] });
  expect(fetcher).not.toHaveBeenCalled();
  stop();
});

it('reads the new answer and only the target named by server-provided cids', async () => {
  const comment = { id: 'comment', cid: 'c1.1', user_message_id: 'user', quote: 'text', note: '', from_off: 0, to_off: 4, created_at: 't', answers: [] };
  messages.set([row('target', 1, { chat_comments: [comment] }), row('unrelated', 2), row('answer', 3)]);
  const batches: string[][] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    const ids = JSON.parse(String(options.body)).message_ids as string[];
    batches.push(ids);
    return { ok: true, json: async () => ({ messages: ids.map(id => id === 'answer' ? { id, chat_answer_cids: ['c1.1'], chat_visible_content: '' } : { id, chat_comments: [comment] }) }) };
  }));
  const stop = watchChatCommentThreads(50);
  await state.handlers.get('turn_complete')!({ thread_id: 'thread', step_ids: ['answer'] });
  expect(batches).toEqual([['answer'], ['target']]);
  stop();
});

it('refreshes a loaded target that did not yet have its comment when the answer arrives', async () => {
  const comment = { id: 'comment', cid: 'c1.1', user_message_id: 'user', quote: 'text', note: '', from_off: 0, to_off: 4, created_at: 't', answers: [{ assistant_message_id: 'answer', text: 'answer', created_at: 't', unread: true }] };
  // The notice points to the oldest unread answer, which may be another target.
  const reply = { detail: { destination: { id: 'other', cid: 'c0.1' } } };
  messages.set([row('target', 1), { ...row('user', 2), role: 'user', metadata: { reply_context: { comments: [{ seq: 1, cid: 'c1.1' }] } } }, row('answer', 3)] as never);
  const batches: string[][] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    const ids = JSON.parse(String(options.body)).message_ids as string[];
    batches.push(ids);
    return { ok: true, json: async () => ({ chat_reply_items: [reply], messages: ids.map(id => id === 'answer' ? { id, chat_answer_cids: ['c1.1'] } : { id, chat_comments: [comment] }) }) };
  }));
  const stop = watchChatCommentThreads(50);
  await state.handlers.get('turn_complete')!({ thread_id: 'thread', step_ids: ['answer'] });
  expect(batches).toEqual([['answer'], ['target']]);
  expect(get(messages).find(item => item.id === 'target')?.chat_comments?.[0].answers[0].text).toBe('answer');
  stop();
});

it('refreshes a root when a reply answer arrives after its sending message has left the page', async () => {
  const comment = { id: 'root', cid: 'c1.1', user_message_id: 'sender', quote: 'text', note: '', from_off: 0, to_off: 4, created_at: 't', answers: [{ author_kind: 'user', user_message_id: 'reply-sender', cid: 'c2.1', text: 'reply', created_at: 't', unread: false }] };
  messages.set([row('target', 1, { chat_comments: [comment] }), row('answer', 3)] as never);
  const batches: string[][] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    const ids = JSON.parse(String(options.body)).message_ids as string[];
    batches.push(ids);
    return { ok: true, json: async () => ({ messages: ids.map(id => id === 'answer' ? { id, chat_answer_cids: ['c2.1'] } : { id, chat_comments: [comment] }) }) };
  }));
  const stop = watchChatCommentThreads(50);
  await state.handlers.get('turn_complete')!({ thread_id: 'thread', step_ids: ['answer'] });
  expect(batches).toEqual([['answer'], ['target']]);
  stop();
});

it('refreshes only a newly commented target on input', async () => {
  messages.set([row('target', 1), row('other', 2)]);
  const batches: string[][] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    const ids = JSON.parse(String(options.body)).message_ids as string[];
    batches.push(ids);
    return { ok: true, json: async () => ({ messages: ids.map(id => ({ id })) }) };
  }));
  const stop = watchChatCommentThreads(50);
  state.handlers.get('message_added')!({ thread_id: 'thread', message: { metadata: { reply_context: { comments: [{ seq: 1 }] } } } });
  await vi.waitFor(() => expect(batches).toEqual([['target']]));
  stop();
});
