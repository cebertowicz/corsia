import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import FocusProofSurface from '$lib/__tests__/fixtures/FocusProofSurface.svelte';
import { THREAD, CAUSE, input, work, suppressed, summary, said, card, tool } from '$lib/__tests__/fixtures/focusPauseProof';
import { activeThreadId, activeToolsByThread, messages, streamContentByThread, type Message } from '$lib/stores/threads';
import { coreSettings } from '$lib/stores/settings';
import { token } from '$lib/stores/auth';
import { threadUpdateCards, visitDividers, openAtDivider } from '$lib/stores/threadUpdateCards';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { _resetTurnStateForTests, INITIAL_TURN_STATE, restoreTurnState } from '$lib/ws/turnState';
import { _resetLiveStepForTests } from './plans/planLiveStep';
import { buildMessageListPresentation, buildTranscriptPresentation, buildVisibleMessages, type TranscriptMessage } from './messageListModel';
import { sourceMembers } from './contextEventModel';
import { isAwaitingDelivery } from '$lib/messageDelivery';
import { attachPlanLimitPauses, planLimitPauseDescriptors } from './planLimitPause';
import { mergeTranscriptUpdates } from './transcriptUpdates';
import { countVisibleEntries, runTokens } from './transcriptFocusModel';

// Only effect transports are mocked; all shaping, filtering, placement and rendering are real.
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn(), identifyUser: vi.fn() }));
vi.mock('$lib/ws', () => ({ on: () => () => {}, connect: vi.fn(), disconnect: vi.fn(), isConnected: () => false, reconnectNow: vi.fn(), sendMessage: vi.fn(), stopTurn: vi.fn(), hasActiveTurn: () => false, subscribeThread: vi.fn(), unsubscribeThread: vi.fn(), watchThread: vi.fn(), cancelThreadTypewriters: vi.fn() }));
const summaries = { old: summary('completed'), p: summary('cancelled', 0, CAUSE), n: summary('running') };
const context = { threadId: THREAD, phase: 'active' as const, turnId: 'n' };
const active = { ...INITIAL_TURN_STATE, phase: 'active' as const, turnId: 'n', turnOrder: 1 };
const baseline = () => [work('old1', 1, 'old'), suppressed('p2', 2, 'p'), work('n3', 3, 'n')];
function future(seqShape: 'absent' | 'null' = 'absent'): TranscriptMessage {
 const row = { ...input('future', null, 'n'), delivery_state: 'queued', write_outcome: null,
  delivery_error: null, delivery_revision: 1, _optimistic: false, metadata: { sender_kind: 'agent' } };
 if (seqShape === 'absent') delete (row as Partial<TranscriptMessage>).seq;
 return row;
}
function compose(rows: TranscriptMessage[], cards: ReturnType<typeof card>[] = []) {
 const raw = rows.filter((row) => row.thread_id === THREAD);
 const pauses = planLimitPauseDescriptors(raw, summaries);
 const shaped = buildMessageListPresentation(raw, pauses.map((pause) => pause.source));
 const main = shaped.visibleMessages.filter((row) => !isAwaitingDelivery(row, true));
 const entries = mergeTranscriptUpdates(buildTranscriptPresentation(main, true, { turnId: 'n' }), cards, { focusMode: true, divider: null });
 return { pauses, main, entries, result: attachPlanLimitPauses(entries, pauses, context) };
}
function internal(rows: TranscriptMessage[]) {
 const built = compose(rows);
 expect(built.result.unavailable).toEqual([]);
 expect(built.result.entries).toHaveLength(1);
 expect(built.result.entries[0]).toMatchObject({ kind: 'fold', pauses: [{ afterItem: 0, showProxy: true, pause: { source: rows[1] } }] });
 expect(built.pauses[0].source).toBe(rows[1]);
 return built;
}
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
beforeEach(() => {
 host = document.createElement('div'); document.body.appendChild(host);
 _resetTurnStateForTests(); _resetLiveStepForTests(); restoreTurnState(THREAD, active);
 activeThreadId.set(THREAD); activeToolsByThread.set(new Map()); streamContentByThread.set(new Map());
 messages.set([]); coreSettings.set({ features: { focus_mode: true } } as never);
 token.set(null); threadUpdateCards.set({}); visitDividers.set({}); openAtDivider.set(null); checkpointsByThread.set(new Map());
 vi.stubGlobal('fetch', vi.fn(() => { throw new Error('UNADMITTED_NETWORK_EFFECT'); }));
});
afterEach(async () => {
 if (component) await unmount(component); component = null; host.remove();
 _resetTurnStateForTests(); _resetLiveStepForTests(); activeThreadId.set(null);
 activeToolsByThread.set(new Map()); streamContentByThread.set(new Map()); messages.set([]);
 coreSettings.set(null); token.set(null); threadUpdateCards.set({}); visitDividers.set({}); openAtDivider.set(null); checkpointsByThread.set(new Map()); vi.unstubAllGlobals();
});
async function show(rows: TranscriptMessage[]) {
 messages.set(rows as Message[]);
 component = mount(FocusProofSurface, { target: host, props: { summaries, historyComplete: true } });
 flushSync(); await tick(); flushSync();
}
async function replace(rows: TranscriptMessage[]) { messages.set(rows as Message[]); flushSync(); await tick(); flushSync(); }
function mainToggles() { return [...host.querySelectorAll<HTMLButtonElement>('.fold-toggle')].filter((button) => !button.closest('.pending-input-region')); }
function marker() { expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(1); }
function expandedOrder() {
 marker();
 const text = host.textContent!;
 expect(text.indexOf('Work old1')).toBeLessThan(text.indexOf('Paused: plan limit reached'));
 expect(text.indexOf('Paused: plan limit reached')).toBeLessThan(text.indexOf('Work n3'));
 expect(text).not.toContain('Routine private context');
 expect(mainToggles()).toHaveLength(1);
}
function disclosure() { mainToggles()[0].click(); flushSync(); }

// Exact title/status universe is frozen separately. Every assertion is ordinary; no expected-failure runner flags.
describe('Focus future input placement', () => {
 for (const shape of ['absent', 'null'] as const) {
  it(`model ${shape} agent suffix preserves the proven internal gap`, () => {
   const rows = baseline(); const before = internal(rows); const claimed = future(shape);
   const after = internal([...rows, claimed]);
   expect(after.main.map((row) => row.id)).toEqual(['old1', 'n3', 'future']);
   expect(sourceMembers(after.main.at(-1)!)[0]).toBe(claimed);
   expect(runTokens(after.result.entries, 0, true).filter((value) => value.startsWith('after:')))
    .toEqual(runTokens(before.result.entries, 0, true).filter((value) => value.startsWith('after:')));
   expect(countVisibleEntries(after.result.entries)).toBe(countVisibleEntries(before.result.entries));
   expect(claimed.seq).toBe(shape === 'null' ? null : undefined);
   expect(claimed.write_outcome).toBeNull();
  });
  it(`mounted ${shape} append and delivered receipt preserve both disclosure states`, async () => {
   const rows = baseline(); await show(rows); marker(); disclosure(); expandedOrder(); disclosure();
   const claimed = future(shape); await replace([...rows, claimed]);
   // Independent DOM failure is reached before any pure-slot assertion after append.
   marker(); disclosure(); expandedOrder(); disclosure(); marker();
   const receipt = { ...claimed, seq: 4, delivery_state: 'delivered', write_outcome: 'accepted', delivery_revision: 2 };
   await replace([...rows, receipt]); marker(); disclosure(); expandedOrder();
   expect(get(messages).find((row) => row.id === 'p2')).toBe(rows[1]);
   expect(get(messages).find((row) => row.id === 'future')).toBe(receipt);
   expect(host.querySelectorAll('.ev-delivery')).toHaveLength(0);
  });
 }
 for (const status of ['ambiguous', 'failed', 'cancelled'] as const) {
  it(`mounted ${status} receipt preserves the pause without an extra anchor strip`, async () => {
   const rows = baseline(); await show(rows); marker();
   const claimed = future(); await replace([...rows, claimed]); marker();
   const receipt = { ...claimed, seq: 4, delivery_state: status, write_outcome: status === 'ambiguous' ? 'ambiguous' : 'proved_no_write',
    delivery_error: status === 'cancelled' ? null : 'Receipt outcome', delivery_revision: 2 };
   await replace([...rows, receipt]); marker(); disclosure(); expandedOrder();
   // Agent receipt warnings belong to its ordinary bubble, never the suppressed p anchor proxy.
   expect(host.querySelectorAll('.ev-delivery')).toHaveLength(0);
   expect(get(messages).find((row) => row.id === 'future')).toMatchObject({ seq: 4, delivery_state: status, delivery_revision: 2 });
  });
 }
 it('model multiple future singletons preserve original predecessor index', () => {
  const rows = baseline(); const second = { ...future(), id: 'future-two' };
  internal([...rows, future(), second]);
 });
 it('model context loss and restoration reaches the same internal host', () => {
  const rows = [...baseline(), future()]; const built = internal(rows);
  expect(attachPlanLimitPauses(built.entries, built.pauses, { ...context, turnId: 'other' }).unavailable).toEqual(built.pauses);
  expect(attachPlanLimitPauses(built.entries, built.pauses, context).unavailable).toEqual([]);
 });
 it('local host inversion 1 4 3 5 rejects an independently valid descriptor', () => {
  const source = suppressed('p2', 2, 'p');
  const pauses = planLimitPauseDescriptors([source], summaries);
  expect(pauses).toHaveLength(1);
  const entries = buildTranscriptPresentation([work('a1', 1, 'old'), work('a4', 4, 'n'), work('a3', 3, 'n'), work('a5', 5, 'n')], true);
  expect(attachPlanLimitPauses(entries, pauses, context).unavailable).toEqual(pauses);
 });
 it('raw F2 invalidation is separate from local host inversion', () => {
  const rows = [work('p1', 1, 'p'), work('p4', 4, 'p'), work('p3', 3, 'p'), work('p5', 5, 'p')];
  expect(planLimitPauseDescriptors(rows, summaries)).toEqual([]);
 });
 for (const kind of ['unknown', 'invalid', 'wrong thread', 'wrong Turn', 'error', 'conflicting owner', 'future then numeric'] as const) {
  it(`local ${kind} suffix never gains future proof`, () => {
   const rows = baseline(); const pauses = planLimitPauseDescriptors(rows, summaries);
   let extra: TranscriptMessage = future();
   if (kind === 'unknown') extra = { ...work('unknown', 4, 'n'), seq: null };
   if (kind === 'invalid') extra = { ...extra, seq: 0 };
   if (kind === 'wrong thread') extra = { ...extra, thread_id: 'other' };
   if (kind === 'wrong Turn') extra = { ...extra, active_turn_id: 'other' };
   if (kind === 'error') extra = { ...extra, delivery_error: 'Refused' };
   if (kind === 'conflicting owner') extra = { ...extra, metadata: { sender_kind: 'agent', turn_id: 'other' } };
   const shown = [rows[0], rows[2], extra, ...(kind === 'future then numeric' ? [work('later4', 4, 'n')] : [])];
   expect(attachPlanLimitPauses(buildTranscriptPresentation(shown, true), pauses, context).unavailable).toEqual(pauses);
  });
 }
 it('local mixed unknown aggregate stays unavailable', () => {
  const rows = baseline(); const pauses = planLimitPauseDescriptors(rows, summaries);
  const group = buildVisibleMessages([tool('t4', 4, 'n'), tool('t5', 5, 'n'), tool('t6', 6, 'n')])[0];
  sourceMembers(group)[1].seq = null;
  expect(attachPlanLimitPauses(buildTranscriptPresentation([rows[0], rows[2], group], true), pauses, context).unavailable).toEqual(pauses);
 });
 for (const kind of ['empty', 'future only'] as const) {
  it(`standalone ${kind} entry keeps before geometry`, () => {
   const pauses = planLimitPauseDescriptors([suppressed('p2', 2, 'p')], summaries);
   const entries = buildTranscriptPresentation(kind === 'empty' ? [] : [future()], true, { turnId: 'n' });
   const result = attachPlanLimitPauses(entries, pauses, context);
   expect(result.unavailable).toEqual([]);
   expect(result.entries.map((entry) => entry.kind)).toEqual(['pause', 'fold']);
   expect(countVisibleEntries(result.entries)).toBe(countVisibleEntries(entries));
  });
 }
 it('one numeric predecessor plus future supplies no internal successor', () => {
  const pauses = planLimitPauseDescriptors([suppressed('p2', 2, 'p')], summaries);
  const entries = buildTranscriptPresentation([work('old1', 1, 'old'), future()], true);
  expect(attachPlanLimitPauses(entries, pauses, context).unavailable).toEqual(pauses);
 });
 it('valid numeric aggregate retains its original internal predecessor', () => {
  const source = suppressed('p3', 3, 'p'); const pauses = planLimitPauseDescriptors([source], summaries);
  const rows = buildVisibleMessages([tool('t1', 1, 'old'), tool('t2', 2, 'old'), work('n4', 4, 'n')]);
  const result = attachPlanLimitPauses(buildTranscriptPresentation(rows, true), pauses, context);
  expect(result.unavailable).toEqual([]);
  expect(result.entries[0]).toMatchObject({ kind: 'fold', pauses: [{ afterItem: 0, pause: { source } }] });
 });
 it('for_user equal fragments in separate entries retain the final marker', () => {
  const row = said('final2', 2, 'p', true); const pauses = planLimitPauseDescriptors([row], summaries);
  const entries = mergeTranscriptUpdates(buildTranscriptPresentation([row, work('n3', 3, 'n')], true), [card(10, 2)], { focusMode: true, divider: null });
  const result = attachPlanLimitPauses(entries, pauses, context);
  expect(result.unavailable).toEqual([]);
  expect(result.entries.map((entry) => entry.kind)).toEqual(['fold', 'message', 'pause', 'update', 'fold']);
 });
 it('equal card cut preserves standalone ordering through future append and receipt', () => {
  const rows = baseline(); const claimed = future();
  for (const tail of [claimed, { ...claimed, seq: 4, delivery_state: 'delivered', write_outcome: 'accepted', delivery_revision: 2 }]) {
   const built = compose([...rows, tail], [card(10, 2)]);
   expect(built.result.unavailable).toEqual([]);
   expect(built.result.entries.map((entry) => entry.kind)).toEqual(['fold', 'pause', 'update', 'fold']);
  }
 });
 for (const kind of ['wrong thread', 'pending'] as const) {
  it(`mounted ${kind} input is filtered away without hiding history`, async () => {
   const rows = baseline(); await show(rows); marker();
   const extra = kind === 'wrong thread' ? { ...future(), thread_id: 'other' } : { ...future(), active_turn_id: null };
   await replace([...rows, extra]); marker(); disclosure(); expandedOrder();
  });
 }
});
