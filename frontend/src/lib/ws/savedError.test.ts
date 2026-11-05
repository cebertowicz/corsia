import { beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages, type Message } from '../stores/threads';
import { mergeFetchedHistoryRows } from '../messageHistory';
import { handleMessage } from './handlers';
import { handleAuthOk, handleWsError } from './handlersConnection';
import { handleTurnActivity } from './handlersTurns';
import { _resetTurnStateForTests } from './turnState';

const saved: Message = { id: 'saved', thread_id: 'thread', role: 'system', content: 'saved notice', seq: 1, created_at: '2026-01-01T00:00:00Z', metadata: { type: 'llm_error', turn_id: 'one', reference: 'ref' } };
const error = { thread_id: 'thread', turn_id: 'one', turn_order: 1, message: 'failure', reference: 'ref' };
const start = (id: string, order: number) => handleTurnActivity({ thread_id: 'thread', turn_id: id, turn_order: order });

beforeEach(() => { activeThreadId.set('thread'); messages.set([]); _resetTurnStateForTests(); });

describe('saved Turn error notices', () => {
 it.each(['message_added', 'messages_added'])('shows only the saved row when frame precedes %s', (type) => {
  start('one', 1); handleWsError(error);
  expect(get(messages)).toEqual([]);
  handleMessage({ type, thread_id: 'thread', ...(type === 'message_added' ? { message: saved } : { messages: [saved] }) }, () => {});
  expect(get(messages).map(row => row.id)).toEqual(['saved']);
 });
 it('keeps the saved notice when it precedes the frame and when the next Turn starts', () => {
  start('one', 1); messages.set([saved]); handleWsError(error); start('two', 2);
  expect(get(messages).map(row => row.id)).toEqual(['saved']);
  expect(mergeFetchedHistoryRows([], [saved]).map(row => row.id)).toEqual(['saved']);
 });
 it('keeps an unsaved error through reconnect and a later Turn', () => {
  start('one', 1); handleWsError({ ...error, reference: undefined });
  handleAuthOk({ user: null, active_turns: [{ thread_id: 'thread', turn_id: 'two', turn_order: 2 }] }, vi.fn());
  expect(get(messages)).toHaveLength(1);
 });
 it.each([
  { thread_id: 'thread', message: 'uncorrelated' },
  { thread_id: 'thread', input_id: 'input', message: 'rejected' },
  { thread_id: 'thread', turn_id: 'one', turn_order: 1, error_type: 'stop_failed', message: 'stop failed' },
 ])('retains unsaved notice %# after the next Turn', (frame) => {
  start('one', 1); handleWsError(frame); start('two', 2);
  expect(get(messages)).toHaveLength(1);
 });
});
