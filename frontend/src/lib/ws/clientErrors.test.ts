import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages, type Message } from '../stores/threads';
import { mergeFetchedHistoryRows } from '../messageHistory';
import { handleMessage } from './handlers';
import { handleWsError } from './handlersConnection';
import { handleTurnActivity } from './handlersTurns';
import { _resetTurnStateForTests } from './turnState';
import { settledClientErrors } from './clientErrors';

const saved: Message = { id: 'saved', thread_id: 'thread', role: 'system', content: 'saved notice', seq: 1, created_at: '2026-01-01T00:00:00Z', metadata: { type: 'llm_error', turn_id: 'one' } };
const error = { thread_id: 'thread', turn_id: 'one', turn_order: 1, message: 'failure' };
const start = (id: string, order: number) => handleTurnActivity({ thread_id: 'thread', turn_id: id, turn_order: order });

beforeEach(() => { activeThreadId.set('thread'); messages.set([]); _resetTurnStateForTests(); });

describe('Turn error notices', () => {
 it('keeps the saved notice when it precedes the error frame', () => {
  start('one', 1); messages.set([saved]); handleWsError(error);
  expect(get(messages).map(row => row.id)).toEqual(['saved']);
 });
 it('settles the client notice when a saved row arrives in messages_added', () => {
  start('one', 1); handleWsError(error); const client = get(messages)[0];
  handleMessage({ type: 'messages_added', thread_id: 'thread', messages: [saved] }, () => {});
  expect(get(messages).map(row => row.id)).toEqual(['saved']);
  expect(settledClientErrors.has(client)).toBe(true);
 });
 it('settles a client notice when a single saved row is broadcast', () => {
  start('one', 1); handleWsError(error); const client = get(messages)[0];
  handleMessage({ type: 'message_added', thread_id: 'thread', message: saved }, () => {});
  expect(get(messages).map(row => row.id)).toEqual(['saved']);
  expect(settledClientErrors.has(client)).toBe(true);
 });
 it('does not correlate a saved row from a different Turn or thread', () => {
  start('one', 1); handleWsError(error); const client = get(messages)[0];
  const otherTurn = { ...saved, metadata: { type: 'llm_error', turn_id: 'two' } };
  const otherThread = { ...saved, id: 'other', thread_id: 'other' };
  expect(mergeFetchedHistoryRows([client], [otherTurn, otherThread])).toContain(client);
  expect(settledClientErrors.has(client)).toBe(false);
 });
 it('settles the client notice on history merge', () => {
  start('one', 1); handleWsError(error); const client = get(messages)[0];
  expect(mergeFetchedHistoryRows([client], [saved]).map(row => row.id)).toEqual(['saved']);
  expect(settledClientErrors.has(client)).toBe(true);
 });
 it('removes the previous Turn notice when a later Turn starts', () => {
  start('one', 1); handleWsError(error); const client = get(messages)[0]; start('two', 2);
  expect(get(messages)).toEqual([]);
  expect(settledClientErrors.has(client)).toBe(false);
 });
 it.each([
  { thread_id: 'thread', message: 'uncorrelated' },
  { thread_id: 'thread', input_id: 'input', message: 'rejected' },
  { thread_id: 'thread', turn_id: 'one', turn_order: 1, error_type: 'stop_failed', message: 'stop failed' },
 ])('retains uncorrelated notice %# after the next Turn', (frame) => {
  start('one', 1); handleWsError(frame); start('two', 2);
  expect(get(messages)).toHaveLength(1);
  expect(get(messages)[0]._clientErrorTurnId).toBeUndefined();
 });
});
