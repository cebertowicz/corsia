// PREPARATION ONLY. Never imported/run in this preparation turn.
// Intended future destination: frontend/src/lib/__tests__/fixtures/focusPauseProof.ts.
import type { TranscriptMessage } from '$lib/components/messageListModel';
import type { UpdateCard, VisitDivider } from '$lib/api/threadUpdates';
import type { TurnSummary } from '$lib/components/transcriptRunLabel';
export const THREAD = 'focus-pause-proof';
export const CAUSE = 'plan_limit_overage_off';
export const DAY = '2026-10-02T';
export type Anchor = { turn: string; id: string; kind: 'work' | 'input' };
export type Case = {
  name: string; rows: TranscriptMessage[]; cards: UpdateCard[];
  summaries: Record<string, TurnSummary>; live: string | null;
  groups: string[][]; shown: number[]; anchors: Anchor[];
  unavailable?: string[]; divider?: VisitDivider; note?: string;
};
export function summary(status: string, count = 0, cause: string | null = null): TurnSummary {
  return { started_at: DAY + '10:00:00Z', ended_at: status === 'running' ? null : DAY + '10:10:00Z', status, activity_count: count, cause };
}
export function input(id: string, seq: number | null, owner: string | null, role = 'user'): TranscriptMessage {
  return { id, thread_id: THREAD, role, content: `Input ${id}`, seq,
    created_at: DAY + '10:00:00Z', write_started_at: DAY + '10:00:00Z',
    activity_weight: 0, active_turn_id: owner, delivery_state: 'delivered',
    write_outcome: 'accepted', metadata: {} };
}
export function work(id: string, seq: number, owner: string, weight: number | undefined = 1): TranscriptMessage {
  return { id, thread_id: THREAD, role: 'assistant', content: `Work ${id}`, seq,
    created_at: DAY + '10:01:00Z', activity_weight: weight, metadata: { turn_id: owner } };
}
export function auto(id: string, seq: number | null, stopped: string, owner: string | null): TranscriptMessage {
  return { ...input(id, seq, owner, 'system'), content: 'Continue the work that turn was doing.',
    metadata: { type: 'context_event', event_type: CAUSE, cause: CAUSE,
      event_disposition: 'wake', sender_kind: 'system', stopped_turn_id: stopped,
      summary: 'Resumed after plan limit pause' } };
}
export function compact(id: string, seq: number, owner: string | null): TranscriptMessage {
  return { ...input(id, seq, owner, 'system'), metadata: { type: 'context_event',
    event_type: 'context.changed', event_disposition: 'passive', summary: `Context ${id}` } };
}
export function suppressed(id: string, seq: number, owner: string): TranscriptMessage {
  return { ...compact(id, seq, owner), metadata: { type: 'context_event', event_type: 'context.digest',
    event_disposition: 'passive', context_events: [{ id: `${id}-detail`, summary: 'Routine private context', chip: null }] } };
}
export function tool(id: string, seq: number, owner: string): TranscriptMessage {
  return { ...work(id, seq, owner), content: '', metadata: { turn_id: owner,
    tool_calls: [{ id: `call-${id}`, name: 'read_file', arguments: '{}', result: `Result ${id}` }] } };
}
export function artifact(id: string, seq: number): TranscriptMessage {
  return { id, thread_id: THREAD, role: 'system', content: `Removed ${id}`, seq,
    created_at: DAY + '10:01:00Z', activity_weight: 0,
    metadata: { type: 'artifact_removed', subtype: 'hidden_from_pane', causal_event_id: 'batch-one', title: id } };
}
export function said(id: string, seq: number, owner: string, activity = false): TranscriptMessage {
  return { ...work(id, seq, owner, activity ? 1 : 0), content: `Said ${id}`,
    reasoning: activity ? `Reasoning ${id}` : undefined,
    metadata: { turn_id: owner, for_user: true, ...(activity ? { tool_calls: [{ id: `call-${id}`, name: 'read_file', arguments: '{}', result: 'Done' }] } : {}) } };
}
export function card(id: number, after: number, node = `node-${id}`): UpdateCard {
  return { id, thread_id: THREAD, kind: 'failed', node_id: node, title: `Card ${id}`,
    after_seq: after, created_at: DAY + '10:05:00Z', node_exists: false, asks_user: true, blocked_asks_user: true };
}
const pn = () => ({ p: summary('cancelled', 1, CAUSE), n: summary('running') });
function make(name: string, rows: TranscriptMessage[], groups: string[][], anchors: Anchor[], cards: UpdateCard[] = [], overrides: Partial<Case> = {}): Case {
  return { name, rows, groups, anchors, cards, summaries: pn(), live: 'n',
    shown: cards.map(c => c.id), ...overrides };
}
const a = (id: string, kind: 'work' | 'input' = 'work', turn = 'p'): Anchor => ({ id, kind, turn });
export function cases(): Case[] {
  const compactRows = () => [compact('c1', 1, 'old'), compact('c2', 2, 'old'), compact('c3', 3, 'p'), auto('auto4', 4, 'p', 'n'), work('n5', 5, 'n')];
  return [
    make('58 automatic continuation, one live run', [input('ask1', 1, 'p'), work('p2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['p2', 'auto3', 'n4']], [a('p2')]),
    make('G8 visible human continuation', [input('ask1', 1, 'p'), work('p2', 2, 'p'), input('continue3', 3, 'n'), work('n4', 4, 'n')], [['p2'], ['n4']], [a('p2')]),
    make('G8 human input only zero activity', [input('ask1', 1, 'p'), input('continue2', 2, 'n')], [[]], [a('ask1', 'input')], [], { summaries: { p: summary('cancelled', 0, CAUSE), n: summary('running', 0) } }),
    make('70 persisted reasoning source', [{ ...work('reason1', 1, 'p'), content: '', reasoning: 'Persisted reasoning' }, auto('auto2', 2, 'p', 'n'), work('n3', 3, 'n')], [['reason1', 'auto2', 'n3']], [a('reason1')]),
    make('59 successor before first output', [input('ask1', 1, 'p'), work('p2', 2, 'p'), auto('auto3', 3, 'p', 'n')], [['p2', 'auto3']], [a('p2')]),
    make('59 both before first output', [input('ask1', 1, 'p'), auto('auto2', 2, 'p', 'n')], [['auto2']], [a('ask1', 'input')], [], { summaries: { p: summary('cancelled', 0, CAUSE), n: summary('running', 0) } }),
    make('60 compact end anchor equal card', compactRows(), [['c1', 'c2', 'c3'], ['auto4', 'n5']], [a('c3', 'input')], [card(10, 3)]),
    make('61 inside compact card', compactRows(), [['c1', 'c2'], ['c3', 'auto4', 'n5']], [a('c3', 'input')], [card(10, 2)]),
    make('62 tool aggregate inside card', [tool('t1', 1, 'p'), tool('t2', 2, 'p'), tool('t3', 3, 'p'), auto('auto4', 4, 'p', 'n'), work('n5', 5, 'n')], [['t1', 't2'], ['t3', 'auto4', 'n5']], [a('t3')], [card(10, 2)], { summaries: { p: summary('cancelled', 3, CAUSE), n: summary('running', 1) } }),
    make('62 turnless artifact causal aggregate', [artifact('ar1', 1), artifact('ar2', 2), artifact('ar3', 3)], [['ar1', 'ar2'], ['ar3']], [], [card(10, 2)], { live: null, summaries: {} }),
    make('63 suppressed input no containing run equal card', [said('old1', 1, 'old'), suppressed('s2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['auto3', 'n4']], [a('s2', 'input')], [card(10, 2)]),
    make('63 suppressed input card before slot', [said('old1', 1, 'old'), suppressed('s2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['auto3', 'n4']], [a('s2', 'input')], [card(10, 1)]),
    make('64 suppressed input inside existing run', [work('old1', 1, 'old'), suppressed('s2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['old1', 'auto3', 'n4']], [a('s2', 'input')]),
    make('64 card leaves suppressed slot in neither run', [work('old1', 1, 'old'), suppressed('s2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['old1'], ['auto3', 'n4']], [a('s2', 'input')], [card(10, 2)]),
    make('65 for_user final fragment before equal card', [input('ask1', 1, 'p'), work('p2', 2, 'p'), said('final3', 3, 'p', true), auto('auto4', 4, 'p', 'n'), work('n5', 5, 'n')], [['p2', 'final3'], ['auto4', 'n5']], [a('final3')], [card(10, 3)]),
    make('66 two pauses one distinct live successor', [input('ask1', 1, 'p'), work('p2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n'), auto('auto5', 5, 'n', 'q'), work('q6', 6, 'q')], [['p2', 'auto3', 'n4', 'auto5', 'q6']], [a('p2'), a('n4', 'work', 'n')], [], { live: 'q', summaries: { p: summary('cancelled', 1, CAUSE), n: summary('cancelled', 1, CAUSE), q: summary('running', 1) } }),
    make('66 tied equal cards numeric id order', [work('p1', 1, 'p'), auto('auto2', 2, 'p', 'n'), said('n3', 3, 'n', true), auto('auto4', 4, 'n', 'q'), work('q5', 5, 'q')], [['p1', 'auto2', 'n3'], ['auto4', 'q5']], [a('p1'), a('n3', 'work', 'n')], [card(12, 3), card(11, 3)], { shown: [11, 12], live: 'q', summaries: { p: summary('cancelled', 1, CAUSE), n: summary('cancelled', 1, CAUSE), q: summary('running', 1) } }),
    make('67 latest per node and tucked selection', [work('p1', 1, 'p'), work('p2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n'), work('n5', 5, 'n'), work('n6', 6, 'n')], [['p1', 'p2'], ['auto3', 'n4', 'n5'], ['n6'], []], [a('p2')], [card(1, 2, 'same'), card(2, 2), card(3, 2), card(4, 2), card(5, 5), card(6, 6, 'same')], { shown: [2, 3, 4, 5, 6] }),
    make('69 partial page contains successor only', [auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['auto3', 'n4']], [], [], { unavailable: ['p'], note: 'stopped_turn_id is not claimant provenance' }),
    make('70 user stop matching resume copy without canonical cause', [input('ask1', 1, 'p'), work('p2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['p2', 'auto3', 'n4']], [], [], { summaries: { p: summary('cancelled', 1), n: summary('running') } }),
    make('71 divider ignores annotation', [input('ask1', 1, 'p'), work('p2', 2, 'p'), auto('auto3', 3, 'p', 'n'), work('n4', 4, 'n')], [['p2', 'auto3', 'n4']], [a('p2')], [], { divider: { since: DAY + '10:04:00Z', first_seq: 4 } }),
    make('81 compound cuts suppressed slot final fragment', [compact('c1', 1, 'old'), compact('c2', 2, 'old'), compact('c3', 3, 'p'), suppressed('s4', 4, 'r'), said('final5', 5, 'n', true), work('n6', 6, 'n')], [['c1', 'c2'], ['c3'], ['final5'], ['n6']], [a('c3', 'input'), a('s4', 'input', 'r')], [card(10, 2), card(11, 3), card(12, 4), card(13, 5)], { summaries: { p: summary('cancelled', 0, CAUSE), r: summary('cancelled', 0, CAUSE), n: summary('running', 2) }, note: 'The shown final5 content closes a card-selection gap: cards10/11/12 remain shown before content5, card13 follows it.' }),
  ];
}
export function settledCases(): Case[] {
  return (['ambiguous', 'failed', 'cancelled'] as const).map(state => {
    const row = { ...auto('settled1', 1, 'old', 'p'), delivery_state: state,
      write_outcome: state === 'ambiguous' ? 'ambiguous' : 'proved_no_write', delivery_error: 'proof refusal' };
    return make(`68 settled claimed ${state} input`, [row, auto('auto2', 2, 'p', 'n'), work('n3', 3, 'n')], [['settled1', 'auto2', 'n3']], [a('settled1', 'input')], [], { summaries: { p: summary('cancelled', 0, CAUSE), n: summary('running', 1) } });
  });
}
export function unavailableCases(): Case[] {
  const variants: [string, TranscriptMessage][] = [
    ['claimed queued old seq', { ...auto('bad1', 1, 'old', 'p'), delivery_state: 'queued', write_outcome: null }],
    ['unclaimed queued null seq', { ...auto('bad1', null, 'old', null), delivery_state: 'queued', write_outcome: null }],
    ['unclaimed cancellation', { ...auto('bad1', 1, 'old', null), delivery_state: 'cancelled', write_outcome: 'proved_no_write' }],
    ['missing delivery state', { ...auto('bad1', 1, 'old', 'p'), delivery_state: undefined, write_outcome: null }],
    ['missing claimant', { ...auto('bad1', 1, 'old', null) }],
    ['contradictory claimant', { ...auto('bad1', 1, 'old', 'p'), metadata: { ...auto('bad1', 1, 'old', 'p').metadata, turn_id: 'other' } }],
    ['optimistic', { ...auto('bad1', 1, 'old', 'p'), _optimistic: true }],
    ['seed', { ...auto('bad1', 1, 'old', 'p'), metadata: { type: 'seed' } }],
    ['routed event user', { ...input('bad1', 1, 'p'), metadata: { routed_event: true } }],
    ['null seq', { ...auto('bad1', null, 'old', 'p') }],
    ['zero seq', { ...auto('bad1', 0, 'old', 'p') }],
    ['fractional seq', { ...auto('bad1', 0.5, 'old', 'p') }],
  ];
  return [
    ...variants.map(([name, row]) => make(`68/70 unavailable ${name}`, [row, auto('auto2', 2, 'p', 'n'), work('n3', 3, 'n')], [], [], [], { unavailable: ['p'], note: 'No positive group-order oracle for an unavailable-position case.' })),
    make('70 unavailable duplicate durable sequence', [auto('bad1', 1, 'old', 'p'), auto('bad-other', 1, 'p', 'n'), work('n3', 3, 'n')], [], [], [], { unavailable: ['p'] }),
    make('70 unavailable unknown output owner', [{ ...work('unknown1', 1, 'unknown'), metadata: {} }, auto('auto2', 2, 'p', 'n'), work('n3', 3, 'n')], [], [], [], { unavailable: ['p'] }),
  ];
}
export function timingRows(type: string | undefined, noticeAt: string, replyAt = '10:10:00Z'): TranscriptMessage[] {
  const rows = [input('ask1', 1, 't'), work('a2', 2, 't'),
    { id: 'notice3', thread_id: THREAD, role: 'system', content: 'Notice', seq: 3,
      created_at: noticeAt === 'invalid' ? 'invalid' : DAY + noticeAt,
      activity_weight: 0, metadata: type === undefined ? {} : { type } },
    work('b4', 4, 't'), said('reply5', 5, 't')];
  rows[1].created_at = DAY + '10:01:00Z'; rows[3].created_at = DAY + '10:05:00Z';
  rows[4].created_at = DAY + replyAt;
  return rows;
}
