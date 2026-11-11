import { describe, expect, it, vi } from 'vitest';
import { refreshCommentRuns } from './refreshCommentRuns';
import type { RunStub } from './focusRuns';

const stub: RunStub = { firstId: 'first', lastId: 'last', firstSeq: 1, lastSeq: 5,
  count: 5, weight: 3, turnIds: ['ordinary', 'comment'], kinds: ['steps'], continuesOlder: true };

describe('refreshing a mixed Focus run', () => {
  it('replaces the stale aggregate with the server projection', async () => {
    let runs = [stub];
    const request = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ runs: [{
      kind: 'run', first_id: 'first', last_id: 'ordinary', first_seq: 1, last_seq: 2,
      count: 2, weight: 1, turn_ids: ['ordinary'], kinds: ['steps'], continues_older: false,
    }] }) });
    await refreshCommentRuns('thread', 'comment', { token: 'token', runs: () => runs,
      replace: (old, next) => { runs = runs.flatMap(run => run === old ? next : [run]); }, request });
    expect(request).toHaveBeenCalledOnce();
    expect(runs).toMatchObject([{ count: 2, weight: 1, turnIds: ['ordinary'], continuesOlder: true }]);
  });

  it('does not apply an out-of-date response after the run changes', async () => {
    let runs = [stub];
    const request = vi.fn().mockImplementation(async () => {
      runs = [{ ...stub, count: 8 }];
      return { ok: true, json: async () => ({ runs: [] }) };
    });
    await refreshCommentRuns('thread', 'comment', { token: 'token', runs: () => runs,
      replace: (old, next) => { runs = runs.flatMap(run => run === old ? next : [run]); }, request });
    expect(runs[0].count).toBe(8);
  });
});
