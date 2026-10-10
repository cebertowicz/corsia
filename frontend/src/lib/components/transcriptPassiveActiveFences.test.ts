import { describe, expect, it } from 'vitest';
import type { UpdateCard } from '$lib/api/threadUpdates';
import { buildTranscriptPresentation, type TranscriptEntry } from './transcriptFocusModel';
import { mergeTranscriptUpdates } from './transcriptUpdates';
import { work } from '$lib/__tests__/fixtures/focusPauseProof';

const card = (id: number, after_seq: number, kind: UpdateCard['kind']): UpdateCard => ({
 id, after_seq, kind, thread_id: 't', title: `Task ${id}`, node_id: `n${id}`,
 node_exists: true, asks_user: true, blocked_asks_user: true, created_at: '2026-10-02T09:00:00.123456Z',
});
const active = (seqs = [2, 3, 4, 5, 6]) => seqs.map((seq, i) => card((i + 1) * 10, seq, i % 2 ? 'blocked' : 'failed'));
const ids = (entries: TranscriptEntry[]) => entries.flatMap(entry => entry.kind === 'passive'
 ? entry.cards.map(item => item.id) : entry.kind === 'update' ? [entry.card.id] : []);
const compose = (cards: UpdateCard[]) => mergeTranscriptUpdates([], cards, { focusMode: true, divider: null });

describe('passive outcomes keep their place among Failed/Blocked cards', () => {
 for (const [label, seq, expected] of [
  ['before', 1, [1, 10, 20, 30, 40, 50]],
  ['inside', 3, [10, 1, 20, 30, 40, 50]],
  ['after', 4, [10, 20, 1, 30, 40, 50]],
 ] as const) it(`places a completion ${label} the ordered Failed/Blocked cards`, () => {
  const entries = compose([...active(), card(1, seq, 'done')]);
  expect(ids(entries)).toEqual(expected);
  expect(entries.filter(entry => entry.kind === 'update').map(entry => entry.card.id)).toEqual([10, 20, 30, 40, 50]);
 });
 for (const [id, expected] of [
  [5, [5, 10, 20, 30, 40, 50]],
  [15, [10, 15, 20, 30, 40, 50]],
  [25, [10, 20, 25, 30, 40, 50]],
 ] as const) it(`uses IDs for equal-sequence passive ${id}`, () => {
  const entries = compose([...active([2, 2, 2, 2, 2]), card(id, 2, 'closed')]);
  expect(ids(entries)).toEqual(expected);
 });
 it('preserves several passive portions, retained active counts and unique collection keys', () => {
  const failures = active([2, 2, 2, 2, 2]);
  failures[0].node_id = 'same'; failures[1].node_id = 'same';
  failures.push(card(60, 2, 'blocked'));
  const entries = compose([...failures, card(5, 2, 'done'), card(25, 2, 'done_follow_up'), card(35, 2, 'closed'), card(65, 2, 'done')]);
  expect(ids(entries)).toEqual([5, 20, 25, 30, 35, 40, 50, 60, 65]);
  const retained = entries.flatMap(entry => entry.kind === 'update' ? [entry] : []);
  expect(retained.find(entry => entry.card.id === 20)?.earlier).toBe(1);
  expect(new Set(entries.map(entry => entry.id)).size).toBe(entries.length);
 });
 it('preserves work ranges when only passive cards are added around a failure collection', () => {
  const raw = buildTranscriptPresentation([1, 2, 3, 4, 5, 6, 7].map(seq => work(`w${seq}`, seq, 'p')), true);
  const before = mergeTranscriptUpdates(raw, active(), { focusMode: true, divider: null });
  const after = mergeTranscriptUpdates(raw, [...active(), card(1, 1, 'done'), card(15, 2, 'closed')], { focusMode: true, divider: null });
  const ranges = (entries: TranscriptEntry[]) => entries.filter(entry => entry.kind === 'fold').map(entry => entry.items.map(item => item.message.seq));
  expect(ranges(after)).toEqual(ranges(before));
  expect(ids(after)).toEqual([1, 10, 15, 20, 30, 40, 50]);
 });
});
