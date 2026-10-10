import { describe, expect, it } from 'vitest';
import type { UpdateCard } from '$lib/api/threadUpdates';
import { work, suppressed, summary, CAUSE } from '$lib/__tests__/fixtures/focusPauseProof';
import { buildTranscriptPresentation, countVisibleEntries, visibleEntryContinuation, runTokens, type TranscriptEntry } from './transcriptFocusModel';
import { planLimitPauseDescriptors } from './planLimitPause';
import { mergeTranscriptUpdates } from './transcriptUpdates';

const since = '2026-10-02T10:00:00Z';
const outcome = (id: number, after_seq: number, fresh = false): UpdateCard => ({
 id, after_seq, thread_id: 't', kind: 'done', node_id: `n${id}`, title: `Task ${id}`,
 created_at: `2026-10-02T${fresh ? '11' : '09'}:00:00Z`, node_exists: true, asks_user: true, blocked_asks_user: true,
});
const shape = (entries: TranscriptEntry[]) => entries.map(e => e.kind === 'fold'
 ? ['work', e.items.map(i => i.message.seq)] : e.kind === 'passive' ? ['passive', e.cards.map(c => c.id)] : [e.kind]);

describe('failed outcomes fence only when the user owns the item', () => {
 const failedAt = (id: number, after_seq: number, asks_user: boolean): UpdateCard => ({ ...outcome(id, after_seq), kind: 'failed', asks_user });
 it('keeps an agent-owned failure in the one passive group instead of splitting it', () => {
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation([work('w1', 1, 'p')], true),
   [outcome(1, 1), failedAt(2, 1, false), outcome(3, 1)], { focusMode: true, divider: null });
  expect(shape(entries)).toEqual([['work', [1]], ['passive', [1, 2, 3]]]);
 });
 it('leaves a user-assigned failure a standalone card that splits the passive group', () => {
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation([work('w1', 1, 'p')], true),
   [outcome(1, 1), failedAt(2, 1, true), outcome(3, 1)], { focusMode: true, divider: null });
  expect(shape(entries)).toEqual([['work', [1]], ['passive', [1]], ['update'], ['passive', [3]]]);
 });
});

describe('passive outcomes respect real composition fences', () => {
 it('keeps old/new cards on opposite sides even at the same after_seq', () => {
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation([work('w1', 1, 'p')], true),
   [outcome(1, 1), outcome(2, 1, true)], { focusMode: true, divider: { since, first_seq: null } });
  expect(shape(entries)).toEqual([['work', [1]], ['passive', [1]], ['divider'], ['passive', [2]]]);
 });
 it('keeps loaded work in one block across the visit fence and marks the block new', () => {
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation([1, 2, 3, 4].map(s => work(`w${s}`, s, 'p')), true),
   [outcome(1, 1), outcome(2, 4, true)], { focusMode: true, divider: { since, first_seq: 3, truncated: true } });
  expect(shape(entries)).toEqual([['work', [1, 2, 3, 4]], ['passive', [1, 2]]]);
  expect(entries.find(e => e.kind === 'fold')).toMatchObject({ new: true, divider: { truncated: true, first_seq: 3 } });
 });
 for (const hidden of [false, true]) it(`${hidden ? 'suppressed' : 'loaded'} source pause fences outcomes before relocation`, () => {
  const rows = [work('p1', 1, 'p'), hidden ? suppressed('p2', 2, 'p') : work('p2', 2, 'p'), work('n3', 3, 'n')];
  const pauses = planLimitPauseDescriptors(rows, { p: summary('cancelled', 1, CAUSE) });
  const visible = hidden ? [rows[0], rows[2]] : rows;
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation(visible, true), [outcome(1, 2), outcome(2, 3)],
   { focusMode: true, divider: null, pauses });
  expect(shape(entries)).toEqual([['work', hidden ? [1] : [1, 2]], ['passive', [1]], ['pause'], ['work', [3]], ['passive', [2]]]);
  expect(entries.find(e => e.kind === 'pause')).toMatchObject({ showProxy: hidden, pause: { source: { id: 'p2' } } });
 });
 it('counts appended cards in the same readable group and continues only with no progress', () => {
  let entries = mergeTranscriptUpdates([], [outcome(1, 0)], { focusMode: true, divider: null });
  const again = visibleEntryContinuation(() => entries);
  expect(again()).toBe(true);
  entries = mergeTranscriptUpdates([], [outcome(1, 0), outcome(2, 0)], { focusMode: true, divider: null });
  expect(countVisibleEntries(entries)).toBe(2);
  expect(again()).toBe(false);
 });
 it('keeps opaque multi-Turn stub whole and places the canonical stub pause after its outcomes', () => {
  const stub = { firstId: 's1', lastId: 's6', firstSeq: 1, lastSeq: 6, count: 6, weight: 6, kinds: ['step'], continuesOlder: false, turnIds: ['p', 'n'] };
  const raw = buildTranscriptPresentation([work('later7', 7, 'q')], true, null, undefined, [stub]);
  const entries = mergeTranscriptUpdates(raw, [outcome(1, 2), outcome(2, 7)], {
   focusMode: true, divider: null, turnSummaries: { p: summary('cancelled', 1, CAUSE) },
  });
  expect(entries.map(e => e.kind)).toEqual(['fold', 'passive', 'stub-pause', 'fold', 'passive']);
  const hosts = entries.filter(e => e.kind === 'fold' && e.stub);
  expect(hosts).toHaveLength(1);
  expect(hosts[0]).toMatchObject({ stub, items: [] });
  expect(entries.find(e => e.kind === 'stub-pause')).toMatchObject({ turnId: 'p', after_seq: 6 });
 });
 it('keeps a partially loaded pause source and the whole opaque stub bound', () => {
  const rows = [work('p2', 2, 'p'), work('later7', 7, 'q')];
  const stub = { firstId: 's1', lastId: 's6', firstSeq: 1, lastSeq: 6, count: 6, weight: 6, kinds: ['step'], continuesOlder: false, turnIds: ['p', 'n'] };
  const raw = buildTranscriptPresentation(rows, true, null, undefined, [stub]);
  const entries = mergeTranscriptUpdates(raw, [outcome(1, 2), outcome(2, 7)], {
   focusMode: true, divider: null, pauses: planLimitPauseDescriptors(rows, { p: summary('cancelled', 1, CAUSE) }),
  });
  expect(shape(entries)).toEqual([['work', [2]], ['passive', [1]], ['pause'], ['work', [7]], ['passive', [2]]]);
  expect(entries.find(e => e.kind === 'pause')).toMatchObject({ after_seq: 6, showProxy: false, pause: { source: { id: 'p2', seq: 2 } } });
  expect(entries.filter(e => e.kind === 'fold' && e.stub)).toHaveLength(1);
  expect(entries.find(e => e.kind === 'fold' && e.stub)).toMatchObject({ stub });
 });
 it('keeps a loaded visit suffix and its opaque prefix in one block', () => {
  const stub = { firstId: 's1', lastId: 's2', firstSeq: 1, lastSeq: 2, count: 2, weight: 2, kinds: ['step'], continuesOlder: false, turnIds: ['p'] };
  const raw = buildTranscriptPresentation([3, 4, 5, 6].map(s => work(`w${s}`, s, 'n')), true, null, undefined, [stub]);
  const entries = mergeTranscriptUpdates(raw, [outcome(1, 3), outcome(2, 6, true)], {
   focusMode: true, divider: { since, first_seq: 5 },
  });
  expect(shape(entries)).toEqual([['work', [3, 4, 5, 6]], ['passive', [1, 2]]]);
  expect(entries.filter(e => e.kind === 'fold' && e.stub)).toHaveLength(1);
  expect(entries.find(e => e.kind === 'fold' && e.stub)).toMatchObject({ stub });
 });
 it('never invents a stub interior from a visit sequence or Turn ids', () => {
  const stub = { firstId: 's1', lastId: 's6', firstSeq: 1, lastSeq: 6, count: 6, weight: 6, kinds: ['step'], continuesOlder: false, turnIds: ['p', 'n'] };
  const raw = buildTranscriptPresentation([], true, null, undefined, [stub]);
  const entries = mergeTranscriptUpdates(raw, [outcome(1, 2), outcome(2, 5, true)], {
   focusMode: true, divider: { since, first_seq: 4, truncated: true },
  });
  expect(entries.filter(e => e.kind === 'fold')).toHaveLength(1);
  expect(entries.find(e => e.kind === 'fold')).toMatchObject({ stub, items: [] });
  expect(entries.filter(e => e.kind === 'passive').map(e => e.cards.map(c => c.id))).toEqual([[1, 2]]);
 });
 it('does not guess an unavailable standalone pause bound from a null truncated visit fence', () => {
  const rows = [work('p1', 1, 'p'), suppressed('p2', 2, 'p'), work('n3', 3, 'n')];
  const pauses = planLimitPauseDescriptors(rows, { p: summary('cancelled', 1, CAUSE) });
  // Only the predecessor is loaded: no internal containing gap is proven.
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation([rows[0]], true), [outcome(1, 2), outcome(2, 3, true)], {
   focusMode: true, divider: { since, first_seq: null, truncated: true }, pauses,
  });
  expect(entries.some(e => e.kind === 'pause')).toBe(false);
  expect(entries.filter(e => e.kind === 'fold').flatMap(e => e.items.map(i => i.message.seq))).toEqual([1]);
  expect(entries.find(e => e.kind === 'divider')).toMatchObject({ first_seq: null, truncated: true });
 });
 for (const role of ['user', 'assistant', 'system']) it(`fences outcomes at visible ${role} rows and keeps repeated transitions`, () => {
  const visible = { ...work('boundary3', 3, 'p'), role, metadata: role === 'assistant' ? { for_user: true, turn_id: 'p' } : {} };
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation([work('w1', 1, 'p'), work('w2', 2, 'p'), visible, work('w4', 4, 'p')], true),
   [outcome(1, 1), { ...outcome(2, 2), node_id: 'n1', kind: 'done_follow_up' }, { ...outcome(3, 4), kind: 'closed' }],
   { focusMode: true, divider: null });
  expect(shape(entries)).toEqual([['work', [1, 2]], ['passive', [1, 2]], ['message'], ['work', [4]], ['passive', [3]]]);
  const lastRun = entries.findIndex(e => e.kind === 'fold' && e.items.some(i => i.message.seq === 4));
  expect(runTokens(entries, lastRun, true).some(t => t.includes('passive'))).toBe(false);
 });
});
