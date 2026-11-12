import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { messages } from '$lib/stores/threads';
import { buildTranscriptPresentation, isRunExpanded, runTokens, toggleRun } from './transcriptFocusModel';

const base = { thread_id: 'thread', created_at: '2026-11-09T17:00:00Z' };
const target = {
  ...base, id: 'target', role: 'assistant', content: 'A visible answer', metadata: { for_user: true },
  chat_comments: [{ cid: 'root', user_message_id: 'root-message', answers: [1, 2, 3].map(n => ({
    author_kind: 'user', user_message_id: `send-${n}`, cid: `answer-${n}`,
  })) }],
};
const work = (n: number) => ({ ...base, id: `work-${n}`, role: 'assistant', content: '', metadata: { turn_id: `t${n}` } });
const send = (n: number, options: Record<string, unknown> = {}) => ({
  ...base, id: `send-${n}`, role: 'user', content: '', delivery_state: 'delivered',
  metadata: { reply_context: { comments: [{ id: `comment-${n}`, cid: `answer-${n}`, root_user_message_id: 'root-message', root_cid: 'root' }] } },
  ...options,
});
const last = { ...base, id: 'last', role: 'assistant', content: 'Done', metadata: { for_user: true } };

function presentation(sends: any[]) {
  const rows: any[] = [target];
  sends.forEach((row, i) => rows.push(work(i + 1), row));
  rows.push(work(4), last);
  // Comment sends never reach the transcript, whatever their target or delivery state, so the work around them is one fold.
  messages.set(rows);
  return { entries: buildTranscriptPresentation(get(messages) as any[], true) };
}
afterEach(() => messages.set([]));

describe('Focus comment sends', () => {
  it('joins work across three comment sends', () => {
    const { entries } = presentation([send(1), send(2), send(3)]);
    expect(entries.map(e => e.kind)).toEqual(['message', 'fold', 'message']);
  });
  it('hides a send whose target is unavailable', () => {
    const fallback = send(2, { metadata: { reply_context: { comments: [{ id: 'unplaced', cid: 'unplaced' }] } } });
    const { entries } = presentation([send(1), fallback, send(3)]);
    expect(entries.map(e => e.kind)).toEqual(['message', 'fold', 'message']);
    expect(entries.some(e => e.kind === 'message' && e.message.id === 'send-2')).toBe(false);
  });
  it.each(['queued', 'ambiguous', 'cancelled'])('hides a %s send between work blocks', state => {
    const { entries } = presentation([send(1, { delivery_state: state })]);
    expect(entries.map(e => e.kind)).toEqual(['message', 'fold', 'message']);
  });
  it('keeps a previously expanded inner stub open after merging', () => {
    const stub = (firstId: string, lastId: string, firstSeq: number, lastSeq: number) => ({
      firstId, lastId, firstSeq, lastSeq, count: 2, weight: 2, turnIds: [], kinds: [], continuesOlder: false,
    });
    const stubs = [stub('a', 'b', 2, 3), stub('c', 'd', 5, 6), stub('e', 'f', 8, 9)];
    const rows: any[] = [{ ...target, seq: 1 }, { ...send(1), seq: 4 }, { ...send(2), seq: 7 }, { ...last, seq: 10 }];
    const before = buildTranscriptPresentation(rows, true, null, undefined, stubs);
    const expanded = new Set(runTokens(before, 3, true));
    messages.set(rows);
    const after = buildTranscriptPresentation(get(messages) as any[], true, null, undefined, stubs);
    const tokens = runTokens(after, 1, true);
    expect(isRunExpanded(tokens, expanded)).toBe(true);
    expect(isRunExpanded(tokens, toggleRun(tokens, expanded))).toBe(false);
  });
  it('hides a failed send with an unseen target', () => {
    const failed = send(2, { delivery_state: 'failed', metadata: { reply_context: { comments: [{ id: 'unplaced', root_cid: 'missing', root_user_message_id: 'missing' }] } } });
    const { entries } = presentation([send(1), failed, send(3)]);
    expect(entries.some(e => e.kind === 'message' && e.message.id === 'send-2')).toBe(false);
    expect(entries.map(e => e.kind)).toEqual(['message', 'fold', 'message']);
  });
});
