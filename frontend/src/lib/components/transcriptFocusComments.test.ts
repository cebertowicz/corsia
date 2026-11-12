import { describe, expect, it } from 'vitest';
import { buildTranscriptPresentation, isRunExpanded, runTokens, toggleRun } from './transcriptFocusModel';

// The server never hands chat a comment-only send (backend/tests/test_chat_comment_not_in_chat.py), so the
// transcript holds only the seq each send used. The work on either side of those gaps is one fold.
const base = { thread_id: 'thread', created_at: '2026-11-09T17:00:00Z' };
const target = {
  ...base, id: 'target', seq: 1, role: 'assistant', content: 'A visible answer', metadata: { for_user: true },
  chat_comments: [{ cid: 'root', user_message_id: 'root-message', answers: [1, 2].map(n => ({
    author_kind: 'user', user_message_id: `send-${n}`, cid: `answer-${n}`,
  })) }],
};
const last = { ...base, id: 'last', seq: 10, role: 'assistant', content: 'Done', metadata: { for_user: true } };
const stub = (firstId: string, lastId: string, firstSeq: number, lastSeq: number) => ({
  firstId, lastId, firstSeq, lastSeq, count: 2, weight: 2, turnIds: [], kinds: [], continuesOlder: false,
});

describe('Focus work around comment sends', () => {
  it('joins the server runs on either side of each send into one fold that opens and closes', () => {
    // Sends took seq 4 and 7; the runs around them are 2-3, 5-6 and 8-9.
    const stubs = [stub('a', 'b', 2, 3), stub('c', 'd', 5, 6), stub('e', 'f', 8, 9)];
    const entries = buildTranscriptPresentation([target, last] as any[], true, null, undefined, stubs);
    expect(entries.map(e => e.kind)).toEqual(['message', 'fold', 'message']);
    const tokens = runTokens(entries, 1, true);
    const opened = toggleRun(tokens, new Set());
    expect(isRunExpanded(tokens, opened)).toBe(true);
    expect(isRunExpanded(tokens, toggleRun(tokens, opened))).toBe(false);
  });
});
