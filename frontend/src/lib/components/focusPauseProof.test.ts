// PREPARATION ONLY. Never imported/compiled/run in this preparation.
// Future destination: frontend/src/lib/components/focusPauseProof.test.ts.
// CONTROL, SOURCE-EXPECTED RED and FUTURE REPRESENTATION are classifications,
// not executed results. Ordinary assertions; no it.fails/skip/todo.
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildMessageListPresentation, buildTranscriptPresentation, type TranscriptEntry, type TranscriptMessage } from './messageListModel';
import { mergeTranscriptUpdates } from './transcriptUpdates';
import { countVisibleEntries, visibleEntryContinuation } from './transcriptFocusModel';
import { runLabels, runWeight } from './transcriptRunLabel';
import { isAwaitingDelivery, isImminentDelivery, deliveryPresentation } from '$lib/messageDelivery';
import { activeThreadId, activeToolsByThread, messages, streamContentByThread, type Message } from '$lib/stores/threads';
import { coreSettings } from '$lib/stores/settings';
import { token } from '$lib/stores/auth';
import { threadUpdateCards, visitDividers, openAtDivider } from '$lib/stores/threadUpdateCards';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';
import { _resetLiveStepForTests } from './plans/planLiveStep';
import FocusProofSurface from '$lib/__tests__/fixtures/FocusProofSurface.svelte';
import { leadOpenMs, seedOpenTime } from '$lib/__tests__/fixtures/focusRuns';
import { reactiveProps } from '$lib/__tests__/fixtures/reactiveProps.svelte';
import { needsWakeRetry } from './pendingWakeRetry';
import { THREAD, CAUSE, cases, settledCases, unavailableCases, timingRows, summary, work, input, said, card, tool, auto, type Case } from '$lib/__tests__/fixtures/focusPauseProof';
// Effect transports only. No shaping/card/pause/label/renderer mock.
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn(), identifyUser: vi.fn() }));
vi.mock('$lib/ws', () => ({ on: () => () => {}, connect: vi.fn(), disconnect: vi.fn(), isConnected: () => false, reconnectNow: vi.fn(), sendMessage: vi.fn(), stopTurn: vi.fn(), hasActiveTurn: () => false, subscribeThread: vi.fn(), unsubscribeThread: vi.fn(), watchThread: vi.fn(), cancelThreadTypewriters: vi.fn() }));
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
beforeEach(() => {
  host = document.createElement('div'); document.body.appendChild(host);
  _resetTurnStateForTests(); _resetLiveStepForTests();
  activeThreadId.set(THREAD); activeToolsByThread.set(new Map()); streamContentByThread.set(new Map());
  messages.set([]); coreSettings.set({ features: { focus_mode: true } } as never);
  token.set(null); threadUpdateCards.set({}); visitDividers.set({}); openAtDivider.set(null); checkpointsByThread.set(new Map());
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('UNADMITTED_NETWORK_EFFECT'); }));
});
afterEach(async () => {
  if (component) await unmount(component); component = null; host.remove();
  _resetTurnStateForTests(); _resetLiveStepForTests();
  activeThreadId.set(null); activeToolsByThread.set(new Map()); streamContentByThread.set(new Map());
  messages.set([]); coreSettings.set(null); threadUpdateCards.set({}); visitDividers.set({}); openAtDivider.set(null); checkpointsByThread.set(new Map()); token.set(null); vi.unstubAllGlobals();
});
function chain(c: Case, focusMode = true) {
  // Exact current MessageList ordering: shaping BEFORE delivery filtering.
  const shaped = buildMessageListPresentation(c.rows);
  const main = shaped.visibleMessages.filter(row => !isAwaitingDelivery(row as Message, c.live !== null));
  const imminent = shaped.visibleMessages.filter(row => isImminentDelivery(row as Message, c.live !== null));
  const focused = buildTranscriptPresentation(main, focusMode, c.live ? { turnId: c.live } : null);
  const entries = mergeTranscriptUpdates(focused, c.cards, { focusMode, divider: c.divider ?? null });
  const pending = [
    ...(shaped.pendingContextDigest ? buildTranscriptPresentation([shaped.pendingContextDigest], focusMode, null, 'run:pending-context') : []),
    ...buildTranscriptPresentation(imminent, focusMode),
  ];
  return { shaped, main, focused, entries, pending };
}
const leaves = (entries: TranscriptEntry[]) => entries.flatMap(e => e.kind === 'fold' ? e.items : e.kind === 'message' ? [e] : []);
const idsOf = (row: TranscriptMessage): string[] => row.mergedIds ?? [row.id];
const groups = (entries: TranscriptEntry[]) => entries.flatMap(e => e.kind === 'fold' ? [e.items.flatMap(i => idsOf(i.message))] : []);
const selected = (entries: TranscriptEntry[]) => entries.flatMap(e => e.kind === 'update' ? [e.card.id] : []);
const structural = (entries: TranscriptEntry[]) => entries.flatMap(e => {
  if (e.kind === 'fold') return ['R:' + e.items.flatMap(i => idsOf(i.message)).join(',')];
  if (e.kind === 'message') return ['M:' + e.message.id];
  if (e.kind === 'update') return ['C:' + e.card.id];
  if (e.kind === 'divider') return ['D'];
  return []; // future transparent annotations are not structural boundaries
});
const expectedStructural: Record<string, string[]> = {
  '58 automatic continuation, one live run': ['M:ask1', 'R:p2,auto3,n4'],
  'G8 visible human continuation': ['M:ask1', 'R:p2', 'M:continue3', 'R:n4'],
  'G8 human input only zero activity': ['M:ask1', 'M:continue2', 'R:'],
  '70 persisted reasoning source': ['R:reason1,auto2,n3'],
  '59 successor before first output': ['M:ask1', 'R:p2,auto3'],
  '59 both before first output': ['M:ask1', 'R:auto2'],
  '60 compact end anchor equal card': ['R:c1,c2,c3', 'C:10', 'R:auto4,n5'],
  '61 inside compact card': ['R:c1,c2', 'C:10', 'R:c3,auto4,n5'],
  '62 tool aggregate inside card': ['R:t1,t2', 'C:10', 'R:t3,auto4,n5'],
  '62 turnless artifact causal aggregate': ['R:ar1,ar2', 'C:10', 'R:ar3'],
  '63 suppressed input no containing run equal card': ['M:old1', 'C:10', 'R:auto3,n4'],
  '63 suppressed input card before slot': ['M:old1', 'C:10', 'R:auto3,n4'],
  '64 suppressed input inside existing run': ['R:old1,auto3,n4'],
  '64 card leaves suppressed slot in neither run': ['R:old1', 'C:10', 'R:auto3,n4'],
  '65 for_user final fragment before equal card': ['M:ask1', 'R:p2,final3', 'M:final3', 'C:10', 'R:auto4,n5'],
  '66 two pauses one distinct live successor': ['M:ask1', 'R:p2,auto3,n4,auto5,q6'],
  '66 tied equal cards numeric id order': ['R:p1,auto2,n3', 'M:n3', 'C:11', 'C:12', 'R:auto4,q5'],
  '67 latest per node and tucked selection': ['R:p1,p2', 'C:2', 'C:3', 'C:4', 'R:auto3,n4,n5', 'C:5', 'R:n6', 'C:6', 'R:'],
  '69 partial page contains successor only': ['R:auto3,n4'],
  '70 user stop matching resume copy without canonical cause': ['M:ask1', 'R:p2,auto3,n4'],
  '71 divider ignores annotation': ['M:ask1', 'R:p2,auto3,n4'],
  '81 compound cuts suppressed slot final fragment': ['R:c1,c2', 'C:10', 'R:c3', 'C:11', 'C:12', 'R:final5', 'M:final5', 'C:13', 'R:n6'],
  '68 settled claimed ambiguous input': ['R:settled1,auto2,n3'],
  '68 settled claimed failed input': ['R:settled1,auto2,n3'],
  '68 settled claimed cancelled input': ['R:settled1,auto2,n3'],
};
const structuralReds = new Set(['60', '61', '62', '81']);
const renderControls = new Set(['G8 visible human continuation', 'G8 human input only zero activity', '59 both before first output', '65 for_user final fragment before equal card', '67 latest per node and tucked selection', '69 partial page contains successor only', '70 user stop matching resume copy without canonical cause']);
function assertPositiveGround(c: Case): void {
  const seqs = c.rows.map(r => r.seq as number);
  expect(seqs.every(s => Number.isInteger(s) && s > 0)).toBe(true);
  expect(new Set(seqs).size).toBe(seqs.length);
  expect(seqs.every((s, i) => i === 0 || s > seqs[i - 1])).toBe(true);
  for (const [turn, s] of Object.entries(c.summaries)) if (s.cause === CAUSE) { expect(s.status).toBe('cancelled'); expect(turn).not.toBe(c.live); }
  if (c.live) { expect(c.summaries[c.live].status).toBe('running'); expect(c.summaries[c.live].cause).toBeNull(); }
  for (const a of c.anchors) {
    const row = c.rows.find(r => r.id === a.id)!; expect(row).toBeTruthy();
    expect(row.metadata?.turn_id ?? row.active_turn_id).toBe(a.turn); expect(row._optimistic).not.toBe(true);
    if (a.kind === 'input') expect(['delivered', 'ambiguous', 'failed', 'cancelled']).toContain(row.delivery_state);
  }
}
function assertStructure(c: Case): ReturnType<typeof chain> {
  assertPositiveGround(c); const built = chain(c);
  expect(selected(built.entries)).toEqual(c.shown);
  expect(expectedStructural[c.name]).toBeTruthy(); expect(structural(built.entries)).toEqual(expectedStructural[c.name]);
  expect(groups(built.entries)).toEqual(c.groups);
  expect(built.entries.filter(e => e.kind === 'fold' && e.live)).toHaveLength(c.live ? 1 : 0);
  const represented = leaves(built.entries).flatMap(i => idsOf(i.message));
  for (const row of c.rows) if (row.metadata?.context_events?.every((d: Record<string, unknown>) => d.chip === null)) expect(represented).not.toContain(row.id);
  const rawWeight = c.rows.filter(r => r.role === 'assistant').reduce((n, r) => n + r.activity_weight, 0);
  const builtWeight = built.entries.reduce((n, e) => n + (e.kind === 'fold' ? runWeight(e) ?? NaN : 0), 0);
  expect(builtWeight).toBe(rawWeight); return built;
}
async function render(c: Case, historyComplete = true) {
  messages.set(c.rows as Message[]);
  threadUpdateCards.set({ [THREAD]: { fromSeq: 0, cards: c.cards } });
  visitDividers.set({ [THREAD]: { divider: c.divider ?? null, failed: false } });
  if (c.live) {
    applyLifecycle(THREAD, { turnId: c.live, turnOrder: 1 }, undefined, Date.now());
    activeToolsByThread.set(new Map([[THREAD, [{ id: 'owned-live-tool', name: 'read_file', phrase: 'Reading successor source', status: 'running' }]]]) as never);
  }
  const props = reactiveProps({ summaries: c.summaries, historyComplete, onLoadOlder: () => {} });
  component = mount(FocusProofSurface, { target: host, props }); flushSync(); await tick(); flushSync(); return props;
}
const markers = () => [...host.querySelectorAll<HTMLElement>('.plan-limit-paused')];
const domCards = () => [...host.querySelectorAll<HTMLElement>('[data-update-id]')].map(el => Number(el.dataset.updateId));
const domRows = () => [...host.querySelectorAll<HTMLElement>('[data-message-id]')].filter(el => !el.closest('.pending-input-region'));
function expandWork(): void {
  for (const b of host.querySelectorAll<HTMLButtonElement>('.fold-toggle')) if (!/earlier updates?/.test(b.getAttribute('aria-label') ?? '') && b.getAttribute('aria-expanded') === 'false') b.click();
  flushSync();
}
function precedes(a: Element, b: Element): void {
  expect(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}
function assertExistingMarkerCount(c: Case): void {
  expect(markers().map(m => m.textContent?.trim())).toEqual(c.anchors.map(() => 'Paused: plan limit reached'));
  expect(host.querySelector('.pending-input-region .plan-limit-paused')).toBeNull();
}
function assertExpandedOrder(c: Case, built: ReturnType<typeof chain>): void {
  // Existing selectors and canonical raw facts ONLY. No proposed attributes.
  assertExistingMarkerCount(c);
  // G2: a wake that needs a Retry sits in the pending area, not in the block.
  const actualLeaves = leaves(built.entries).filter(l => !needsWakeRetry(l.message));
  // Omission cannot make the order check vacuously pass. Both for_user phases,
  // including cancelled successors, must have their actual DOM row occurrence.
  expect(domRows().map(el => el.dataset.messageId)).toEqual(actualLeaves.map(l => l.message.id));
  for (const [index, anchor] of c.anchors.entries()) {
    const m = markers()[index]; const source = c.rows.find(r => r.id === anchor.id)!;
    const at = source.seq as number;
    for (const el of domRows()) {
      const representedLeaf = actualLeaves.find(l => l.message.id === el.dataset.messageId);
      expect(representedLeaf, `actual DOM leaf ${el.dataset.messageId} must be in built model`).toBeTruthy();
      const members = idsOf(representedLeaf!.message).map(id => c.rows.find(r => r.id === id)!);
      expect(members.every(Boolean)).toBe(true);
      const beforeOrEqual = members.every(row => row.seq <= at);
      const after = members.every(row => row.seq > at);
      expect(beforeOrEqual || after, 'no represented aggregate may span a canonical pause slot').toBe(true);
      if (beforeOrEqual) precedes(el, m); else precedes(m, el);
      // Both for_user phases share one durable row; BOTH must precede its marker.
    }
    // Includes cancelled n3 in66-tied, inputs, systems and live successors.
    for (const k of c.cards.filter(k => c.shown.includes(k.id))) {
      const el = host.querySelector(`[data-update-id="${k.id}"]`)!; expect(el).toBeTruthy();
      if (k.after_seq >= at) precedes(m, el); else precedes(el, m);
    }
  }
}
function assertLiveHeader(c: Case): void {
  if (!c.live) return;
  const header = host.querySelector<HTMLButtonElement>('.live-work > .fold-toggle');
  expect(header).toBeTruthy(); expect(header?.getAttribute('aria-label')).toContain('current work: Reading successor source');
  expect(header?.textContent).not.toContain('Paused');
}
const allPositive = [...cases(), ...settledCases()];
describe('G3/G5 current-API CHARACTERIZATION, not writer/reachability/resolution proof', () => {
  const controls: [string | undefined, string, string, string[]][] = [
    ['cc_error', '10:03:00Z', '10:10:00Z', ['Worked · 1 step · 3 min', 'Worked · 1 step · 7 min']],
    ...['sandbox_denied', 'user_interrupt', 'Cc_Error', '', undefined].map(type => [type, '10:03:00Z', '10:10:00Z', ['Worked · 1 step', 'Worked · 1 step']] as [string | undefined, string, string, string[]]),
    ['cc_error', 'invalid', '10:10:00Z', ['Worked · 1 step', 'Worked · 1 step']],
    ['cc_error', '10:30:00Z', '10:10:00Z', ['Worked · 1 step · 10 min', 'Worked · 1 step']],
    ['sandbox_denied', '10:30:00Z', '10:10:00Z', ['Worked · 1 step', 'Worked · 1 step']],
    ['cc_error', '10:20:00Z', '10:10:00Z', ['Worked · 1 step · 10 min', 'Worked · 1 step']],
    ['cc_error', '10:20:00Z', '10:08:00Z', ['Worked · 1 step', 'Worked · 1 step']],
  ];
  for (const [type, stamp, replyAt, expected] of controls) it(`CHARACTERIZATION ${String(type)}/${stamp}/${replyAt}`, async () => {
    const c: Case = { name: 'timing', rows: timingRows(type, stamp, replyAt), cards: [], summaries: { t: summary('completed', 2) }, live: null, groups: [['a2'], ['b4']], shown: [], anchors: [] };
    assertPositiveGround(c); expect(c.rows.map(r => r.seq)).toEqual([1, 2, 3, 4, 5]); const built = chain(c);
    expect(built.entries.map(e => e.kind)).toEqual(['message', 'fold', 'message', 'fold', 'message']); expect(groups(built.entries)).toEqual(c.groups);
    const labels = runLabels(built.entries, c.summaries, true, leadOpenMs(c.summaries));
    seedOpenTime(built.entries, c.summaries, { threadId: THREAD });
    expect(built.entries.flatMap(e => e.kind === 'fold' ? [labels.get(e.id)] : [])).toEqual(expected);
    await render(c); expect([...host.querySelectorAll('.fold-toggle')].map(b => b.textContent?.replace('▸', '').trim())).toEqual(expected);
  });
});
describe('G8 built structure: existing APIs, independent literal order before DOM', () => {
  for (const c of allPositive) it(`${structuralReds.has(c.name.split(' ')[0]) ? 'SOURCE-EXPECTED RED' : 'CONTROL'} structure ${c.name}`, () => { assertStructure(c); });
});
describe('G8 real expanded renderer: existing count/leaf/card positions first', () => {
  for (const c of allPositive) it(`${renderControls.has(c.name) ? 'CONTROL' : 'SOURCE-EXPECTED RED'} expanded order ${c.name}`, async () => {
    const built = assertStructure(c); await render(c); expect(domCards()).toEqual(c.shown);
    // Only collapsed Focus suppresses existing wake summaries. Expansion keeps them.
    // A wake needing a Retry names the same summary in the pending area, so the collapsed check cannot tell them apart.
    if (!c.rows.some(needsWakeRetry)) for (const row of c.rows.filter(r => r.metadata?.event_type === CAUSE)) expect(host.textContent).not.toContain(row.metadata.summary);
    assertLiveHeader(c); expandWork(); assertExpandedOrder(c, built);
    for (const row of c.rows.filter(r => r.metadata?.event_type === CAUSE && built.main.some(m => idsOf(m).includes(r.id)))) expect(host.textContent).toContain(row.metadata.summary);
    coreSettings.set({ features: { focus_mode: false } } as never); flushSync();
    expect(domCards()).toEqual([...c.cards].sort((a, b) => a.after_seq - b.after_seq || a.id - b.id).map(c => c.id));
    assertExistingMarkerCount(c);
    coreSettings.set({ features: { focus_mode: true } } as never); flushSync();
    assertExpandedOrder(c, chain(c));
  });
});
describe('G8 FUTURE REPRESENTATION, independent of proposed observation attributes', () => {
  for (const c of allPositive.filter(c => c.anchors.length > 0)) it(`FUTURE REPRESENTATION collapsed historical proxy ${c.name}`, async () => {
    const built = assertStructure(c); await render(c); assertExistingMarkerCount(c); assertLiveHeader(c);
    for (const [i, a] of c.anchors.entries()) {
      const representedIds = leaves(built.entries).filter(l => idsOf(l.message).includes(a.id)).map(l => l.message.id);
      const shownAnchor = domRows().some(el => representedIds.includes(el.dataset.messageId!));
      if (!shownAnchor) expect(markers()[i].previousElementSibling?.textContent).toContain(a.kind === 'input' ? 'Earlier input' : 'Earlier work');
      const containingRun = markers()[i].closest('.transcript-fold');
      if (containingRun) precedes(containingRun.querySelector('.fold-toggle')!, markers()[i]);
    }
  });
  for (const state of ['ambiguous', 'failed', 'cancelled'] as const) it(`SOURCE-EXPECTED RED collapsed ${state} warning`, async () => {
    const c = settledCases().find(c => c.name.includes(state))!; assertStructure(c);
    const delivery = deliveryPresentation(c.rows[0] as Message, true)!; expect(delivery).toBeTruthy(); expect(delivery.label).not.toContain('Delivered');
    const shown = needsWakeRetry(c.rows[0]) ? "Couldn't deliver" : delivery.label;
    await render(c); expect(host.textContent).toContain(shown); assertExistingMarkerCount(c);
    expandWork(); expect(host.textContent).toContain(shown); assertExpandedOrder(c, chain(c));
  });
});
describe('G8 unavailable sources: existing zero-marker controls, including pending region', () => {
  const negativeControls = ['unclaimed queued', 'unclaimed cancellation', 'missing claimant', 'contradictory claimant', 'seed', 'routed event', 'unknown output owner'];
  for (const c of unavailableCases()) it(`${negativeControls.some(s => c.name.includes(s)) ? 'CONTROL' : 'SOURCE-EXPECTED RED'} ${c.name}`, async () => {
    const built = chain(c);
    if (c.name.includes('claimed queued old seq')) expect(built.main.some(r => idsOf(r).includes('bad1'))).toBe(true);
    if (c.name.includes('unclaimed queued')) expect(built.main.some(r => idsOf(r).includes('bad1'))).toBe(false);
    await render(c); expandWork(); expect(markers()).toHaveLength(0); expect(host.querySelector('.pending-input-region .plan-limit-paused')).toBeNull();
  });
});
describe('Conservation and real paging/refetch observables', () => {
  it('CONTROL pending automatic continuation has no historical marker of its own', async () => {
    const base = cases()[0];
    const waiting = { ...auto('waiting3', null, 'p', null), delivery_state: 'queued', write_outcome: null };
    const c = { ...base, rows: [...base.rows.slice(0, 2), waiting] };
    const built = chain(c); expect(built.main.map(r => r.id)).toEqual(['ask1', 'p2']);
    expect(leaves(built.pending).map(l => l.message.id)).toEqual(['waiting3']);
    await render(c); expandWork(); assertExistingMarkerCount(c);
    // G2: a context update that is only waiting is not shown.
    expect(host.querySelector('[data-message-id="waiting3"]')).toBeNull();
    precedes(host.querySelector('[data-message-id="p2"]')!, markers()[0]);
  });
  it('SOURCE-EXPECTED RED settlement of pending continuation keeps historical pause before successor', async () => {
    const base = cases()[0];
    const waiting = { ...auto('waiting3', null, 'p', null), delivery_state: 'queued', write_outcome: null };
    await render({ ...base, rows: [...base.rows.slice(0, 2), waiting] }); expandWork();
    const settled = { ...base, rows: [...base.rows.slice(0, 2), auto('waiting3', 3, 'p', 'n'), work('n4', 4, 'n')] };
    messages.set(settled.rows as Message[]); flushSync(); expandWork();
    expect(leaves(chain(settled).pending)).toHaveLength(0); assertExpandedOrder(settled, chain(settled));
  });
  it('CONTROL unknown vs known-zero tool weight', () => {
    const rows = [tool('t1', 1, 'p'), { ...tool('t2', 2, 'p'), activity_weight: undefined }, tool('t3', 3, 'p')];
    const c: Case = { name: 'unknown weight', rows, cards: [], summaries: {}, live: null, groups: [], shown: [], anchors: [] };
    const run = chain(c).entries.find(e => e.kind === 'fold'); expect(run?.kind).toBe('fold'); if (run?.kind === 'fold') expect(runWeight(run)).toBeNull();
    const zero = chain({ ...c, rows: rows.map(r => ({ ...r, activity_weight: 0 })) }).entries.find(e => e.kind === 'fold'); expect(zero?.kind).toBe('fold'); if (zero?.kind === 'fold') expect(runWeight(zero)).toBe(0);
  });
  it('CONTROL actual rows stay visible through paging, card split, refetch and Focus toggle', async () => {
    const loaded = [11, 12, 13, 14, 15].map(seq => work(`w${seq}`, seq, 't'));
    const older = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(seq => work(`w${seq}`, seq, 't'));
    const c: Case = { name: 'paging observable', rows: loaded, cards: [], summaries: { t: summary('completed', 15) }, live: null, groups: [], shown: [], anchors: [] };
    assertPositiveGround(c); const props = await render(c, false);
    expect(domRows()).toHaveLength(0); host.querySelector<HTMLButtonElement>('.fold-toggle')!.click(); flushSync();
    expect(domRows().map(el => el.dataset.messageId)).toEqual(['w11', 'w12', 'w13', 'w14', 'w15']);
    messages.set([...older, ...loaded] as Message[]); props.historyComplete = true; flushSync();
    expect(domRows().map(el => el.dataset.messageId)).toEqual(['w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8', 'w9', 'w10', 'w11', 'w12', 'w13', 'w14', 'w15']);
    threadUpdateCards.set({ [THREAD]: { fromSeq: 0, cards: [card(10, 12)] } }); flushSync();
    expect([...host.querySelectorAll('.fold-toggle')].map(el => el.getAttribute('aria-expanded'))).toEqual(['true', 'true']);
    const cardEl = host.querySelector('[data-update-id="10"]')!;
    precedes(host.querySelector('[data-message-id="w12"]')!, cardEl); precedes(cardEl, host.querySelector('[data-message-id="w13"]')!);
    const refetched = [...older, ...loaded].map(row => ({ ...row, metadata: { ...row.metadata } }));
    messages.set(refetched as Message[]); flushSync();
    expect(domRows().map(el => el.dataset.messageId)).toEqual(['w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8', 'w9', 'w10', 'w11', 'w12', 'w13', 'w14', 'w15']);
    messages.set([...refetched, said('reply16', 16, 't')] as Message[]); flushSync();
    expect(domRows().map(el => el.dataset.messageId)).toEqual(['w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7', 'w8', 'w9', 'w10', 'w11', 'w12', 'w13', 'w14', 'w15', 'reply16']);
    coreSettings.set({ features: { focus_mode: false } } as never); flushSync();
    coreSettings.set({ features: { focus_mode: true } } as never); flushSync();
    expect(host.querySelector('[data-message-id="w11"]')).toBeTruthy(); expect(host.querySelector('[data-message-id="w15"]')).toBeTruthy();
    expect([...host.querySelectorAll('.fold-toggle')].map(el => el.getAttribute('aria-expanded'))).toEqual(['true', 'true']);
  });
  it('SOURCE-EXPECTED RED raw anchor stays between source/card/successor after older page/refetch', async () => {
    const original = cases().find(c => c.name.startsWith('61'))!;
    const c = { ...original, rows: original.rows.map(r => ({ ...r, seq: (r.seq as number) + 10 })), cards: [{ ...original.cards[0], after_seq: 12 }] };
    const props = await render(c, false); expandWork();
    const paged = { ...c, rows: [input('older1', 1, 'old'), ...c.rows] };
    messages.set(paged.rows as Message[]); props.historyComplete = true; flushSync();
    assertExistingMarkerCount(paged); assertExpandedOrder(paged, chain(paged));
    const m = markers()[0]; precedes(host.querySelector('[data-update-id="10"]')!, m); precedes(m, host.querySelector('[data-message-id="n5"]')!);
    messages.set(paged.rows.map(r => ({ ...r, metadata: { ...r.metadata } })) as Message[]); flushSync();
    assertExpandedOrder(paged, chain(paged)); expect(paged.rows.slice(1).map(r => r.seq)).toEqual([11, 12, 13, 14, 15]);
  });
  it('SOURCE-EXPECTED RED canonical queued-old-seq settlement recomputes marker from existing observables', async () => {
    const c = unavailableCases().find(c => c.name.includes('claimed queued old seq'))!; await render(c); expandWork(); expect(markers()).toHaveLength(0);
    const final = { ...c.rows[0], delivery_state: 'delivered', write_outcome: 'accepted' };
    const refetched = { ...c, rows: [final, ...c.rows.slice(1)], anchors: [{ turn: 'p', id: 'bad1', kind: 'input' as const }] };
    messages.set(refetched.rows as Message[]); flushSync(); assertExpandedOrder(refetched, chain(refetched));
  });
  it('CONTROL visible-count/paging continuation stays tied to shown content', () => {
    const c = cases()[0]; const entries = chain(c).entries; expect(countVisibleEntries(entries)).toBe(2);
    let current = entries; const continuation = visibleEntryContinuation(() => current); expect(continuation()).toBe(true);
    current = chain({ ...c, rows: [...c.rows, said('reply5', 5, 'n')] }).entries; expect(continuation()).toBe(false);
  });
});
