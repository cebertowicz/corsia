import { mount, unmount, flushSync } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { token } from '$lib/stores/auth';
import type { UpdateCard } from '$lib/api/threadUpdates';
import { reactiveProps } from '$lib/__tests__/fixtures/reactiveProps.svelte';
import PassiveOutcomeList from './PassiveOutcomeList.svelte';
import UpdateCardComponent from './UpdateCard.svelte';

const focus = vi.hoisted(() => vi.fn());
const plan = vi.hoisted(() => ({
 status: 'ready', payload: null as unknown, retry: vi.fn(), childRetry: vi.fn(), children: {} as Record<string, { status: string; payload: unknown }>,
}));
vi.mock('$lib/planNodeFocus', () => ({ focusPlanNode: focus }));
vi.mock('$lib/stores/planQuery.svelte', () => ({
 planQuery: (getKey: () => unknown) => ({
  get status() { return getKey() ? plan.status : 'idle'; }, get payload() { return getKey() ? plan.payload : null; }, retry: plan.retry,
 }),
 planKeySet: () => ({ state: (key: { id: string }) => plan.children[key.id] ?? { status: 'idle', payload: null }, retry: plan.childRetry }),
}));
const foldedRead = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/plansFolded', () => ({ readFoldedNode: foldedRead }));
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
const card = (id: number, kind: UpdateCard['kind'] = 'done'): UpdateCard => ({
 id, kind, thread_id: 't', node_id: `node-${id}`, node_exists: true, asks_user: true, blocked_asks_user: false, after_seq: id,
 title: `Original snapshot ${id}`, created_at: `2026-09-29T10:00:0${id}.123456`,
});
let component: Record<string, unknown> | undefined;
let host: HTMLDivElement;
function render(cards: UpdateCard[]) {
 host = document.createElement('div'); document.body.appendChild(host);
 const props = reactiveProps({ cards, id: 'outcomes' });
 component = mount(PassiveOutcomeList, { target: host, props }); flushSync();
 return props;
}
beforeEach(() => { token.set('tok'); foldedRead.mockReset(); foldedRead.mockResolvedValue({ status: 'not-found' }); });
afterEach(async () => {
 if (component) await unmount(component); host?.remove(); focus.mockClear(); plan.retry.mockClear(); plan.childRetry.mockClear();
 plan.status = 'ready'; plan.payload = null; plan.children = {};
});
const planNode = (id: string, parent: string | null, over: Record<string, unknown> = {}) => ({
 id, parent_id: parent, title: `Title ${id}`, description: null, acceptance_criteria: '', child: null, ...over,
});
const planRead = (...nodes: Record<string, unknown>[]) => ({ plans: [{ plan: { id: 'plan-1' }, nodes }] });
const openList = () => { host.querySelector<HTMLButtonElement>('.passive-toggle')!.click(); flushSync(); };
const rowButton = (index = 0) => host.querySelectorAll<HTMLButtonElement>('li > button.row')[index];
const expand = (index = 0) => { rowButton(index).click(); flushSync(); };
const settle = async () => { for (let i = 0; i < 4; i++) { await Promise.resolve(); await tick(); } };

describe('passive task list failures', () => {
 const failedCard = (id: number): UpdateCard => ({ ...card(id, 'failed'), asks_user: false });
 const header = () => host.querySelector('.passive-toggle')!;
 it('counts completed, closed and failed in that order with only the failed segment red-classed', () => {
  render([card(1), card(2, 'closed'), card(3, 'closed'), failedCard(4), failedCard(5)]);
  expect(header().textContent).toContain('1 task completed · 2 closed · 2 failed');
  expect(header().querySelector('.failed-count')!.textContent).toBe('2 failed');
  expect(header().querySelector('svg.closed')).toBeNull();
  expect(header().querySelector('svg.failed')).toBeNull();
  expect(host.querySelector('section')!.getAttribute('aria-label')).toBe('1 task completed · 2 closed · 2 failed');
 });
 it('says tasks failed when only failures are grouped and shows the existing X glyph', () => {
  render([failedCard(1), failedCard(2)]);
  expect(header().textContent).toContain('2 tasks failed');
  expect(header().querySelector('.failed-count')!.textContent).toBe('2 tasks failed');
  expect(header().querySelector('svg.closed.failed')).toBeTruthy();
  host.remove(); render([failedCard(3)]);
  expect(header().textContent).toContain('1 task failed');
 });
 it('keeps existing wording when nothing failed and appends failures to a closed-only list', () => {
  render([card(1, 'closed'), failedCard(2)]);
  expect(header().textContent).toContain('1 task closed · 1 failed');
  expect(header().querySelector('svg.closed')).toBeTruthy();
  expect(header().querySelector('svg.failed')).toBeNull();
 });
 it('renders a failed row with a red X, its own time, and opens its plan item', () => {
  render([card(1), failedCard(2)]);
  const row = host.querySelectorAll('li')[1];
  expect(row.querySelector('.row .failed-count')!.textContent).toBe('Failed');
  expect(row.querySelector('.row svg.failed')).toBeTruthy();
  expect(row.querySelector('.row svg.closed')).toBeNull();
  expect(row.querySelector('time')!.getAttribute('datetime')).toBe(failedCard(2).created_at);
  plan.payload = planRead(planNode('node-2', null));
  openList(); expand(1);
  host.querySelector<HTMLButtonElement>('.open-plan')!.click();
  expect(focus).toHaveBeenCalledWith('t', 'node-2', null);
 });
});

describe('passive task list controls and snapshots', () => {
 it('starts collapsed with an accessible disclosure and real list semantics', () => {
  render([card(1), card(2, 'closed')]);
  const toggle = host.querySelector<HTMLButtonElement>('.passive-toggle')!;
  expect(toggle.textContent).toContain('1 task completed · 1 closed');
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  expect(toggle.getAttribute('aria-controls')).toBe('outcomes');
  expect(host.querySelector('ul')!.hidden).toBe(true);
  expect(host.querySelectorAll('li')).toHaveLength(2);
  expect(host.querySelector('.passive-chevron')?.getAttribute('aria-hidden')).toBe('true');
 });
 it('retains expansion on appended distinct transitions and includes exact original times inside node actions', () => {
  const props = render([card(1), { ...card(2, 'done_follow_up'), node_id: 'node-1' }]);
  host.querySelector<HTMLButtonElement>('.passive-toggle')!.click(); flushSync();
  props.cards = [...props.cards, card(3, 'closed')]; flushSync();
  expect(host.querySelector('.passive-toggle')!.getAttribute('aria-expanded')).toBe('true');
  expect(host.querySelector('ul')!.hidden).toBe(false);
  expect(host.querySelectorAll('li')).toHaveLength(3);
  expect(host.textContent).toContain('Follow-up added');
  const rows = [...host.querySelectorAll('li')];
  expect(rows.map(row => row.querySelector('time')?.getAttribute('datetime'))).toEqual(props.cards.map(c => c.created_at));
  expect(rows[0].querySelector('button.row time')).toBeTruthy();
  expect(rows[0].querySelector('time')!.getAttribute('aria-label')).toContain('2026-09-29T10:00:01.123456');
  plan.payload = planRead(planNode('node-1', null));
  expand(0);
  host.querySelector<HTMLButtonElement>('.open-plan')!.click();
  expect(focus).toHaveBeenCalledWith('t', 'node-1', null);
  expect(rows[0].querySelector('svg.closed')).toBeNull();
  expect(rows[2].querySelector('svg.closed')).toBeTruthy();
 });
 it('shows a neutral closed-only summary and retains a deleted snapshot as plain text', () => {
  render([{ ...card(1, 'closed'), node_exists: false, asks_user: true, title: 'Deleted historical title' }]);
  expect(host.querySelector('.passive-toggle')!.textContent).toContain('1 task closed');
  expect(host.querySelector('.passive-toggle svg.closed')).toBeTruthy();
  expect(host.querySelector('li button')).toBeNull();
  expect(host.querySelector('li')!.textContent).toContain('Deleted historical title');
 });
 it('gives invalid timestamps the same secondary time element without fabricating now', () => {
  render([{ ...card(1), created_at: 'invalid' }]);
  expect(host.querySelector('time')!.textContent).toBe('Time unavailable');
  expect(host.querySelector('time')!.hasAttribute('datetime')).toBe(false);
 });
 it('keeps original timestamps on chronological individual cards when Focus is off', () => {
  host = document.createElement('div'); document.body.appendChild(host);
  component = mount(UpdateCardComponent, { target: host, props: { card: card(1, 'blocked') } }); flushSync();
  expect(host.querySelector('time')!.getAttribute('datetime')).toBe(card(1).created_at);
  expect(host.textContent).toContain('Blocked');
 });
});

describe('completed task rows', () => {
 const rowsText = () => [...host.querySelectorAll('li')].map((li) => li.querySelector('.row')!.textContent!.replace(/\s+/g, ' ').trim());
 it('shows a closed task with a screen-reader prefix and a follow-up outside the truncating title', () => {
  render([card(1, 'closed'), card(2, 'done_follow_up')]);
  const [closedRow, followUp] = host.querySelectorAll('li');
  expect(closedRow.querySelector('.sr-only')!.textContent).toBe('Closed: ');
  expect(closedRow.querySelector('svg.closed')).toBeTruthy();
  expect(followUp.querySelector('.qualifier')!.textContent).toBe('· Follow-up added');
  expect(followUp.querySelector('.node-title')!.textContent).not.toContain('Follow-up');
  expect(followUp.querySelector('.node-title')!.getAttribute('title')).toBe('Original snapshot 2');
 });
 it('formats the time as a clock today and a dated clock earlier, with the zone and date in the name', () => {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 1, 20).toISOString();
  const earlier = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 3, 23, 48).toISOString();
  render([{ ...card(1), created_at: today }, { ...card(2), created_at: earlier }]);
  const [first, second] = [...host.querySelectorAll('time')];
  expect(first.textContent).toMatch(/^\d{1,2}:20\s?(AM|PM)$/i);
  expect(second.textContent).toMatch(/^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:48\s?(AM|PM)$/i);
  expect(second.getAttribute('title')).toMatch(/\d{4}/);
  expect(second.getAttribute('aria-label')).toContain(earlier);
 });
 it('expands in place to the breadcrumb, description and Open in plan, one row at a time', () => {
  plan.payload = planRead(
   planNode('top', null), planNode('mid', 'top'), planNode('parent', 'mid'),
   planNode('node-1', 'parent', { description: 'Why this matters' }), planNode('node-2', null, { acceptance_criteria: 'Tests pass' }),
  );
  render([card(1), card(2)]); openList();
  expect(host.querySelector('.detail')).toBeNull();
  expand(0);
  expect(rowButton(0).getAttribute('aria-expanded')).toBe('true');
  expect(rowButton(0).getAttribute('aria-controls')).toBe(host.querySelector('.detail')!.id);
  expect(host.querySelector('.crumbs')!.getAttribute('aria-label')).toBe('Title top › Title mid › Title parent');
  expect(host.querySelector('.crumb-mid')!.textContent).toContain('Title mid');
  expect(host.querySelector('.text')!.textContent).toBe('Why this matters');
  expand(1);
  expect(host.querySelectorAll('.detail')).toHaveLength(2);
  const second = host.querySelectorAll('.detail')[1];
  expect(second.querySelector('.crumbs')).toBeNull();
  expect(second.querySelector('.text')!.textContent).toBe('Done when: Tests pass');
  expand(0);
  expect(host.querySelectorAll('.detail')).toHaveLength(1);
 });
 it('collapsing the list collapses every expanded row', () => {
  plan.payload = planRead(planNode('node-1', null));
  render([card(1)]); openList(); expand(0);
  expect(host.querySelector('.detail')).not.toBeNull();
  openList(); openList();
  expect(host.querySelector('.detail')).toBeNull();
  expect(rowButton(0).getAttribute('aria-expanded')).toBe('false');
 });
 it('shows only the breadcrumb and Open in plan when there is no description or criteria, and starts at a gap', () => {
  plan.payload = planRead(planNode('b', 'gone'), planNode('node-1', 'b'));
  render([card(1)]); openList(); expand(0);
  expect(host.querySelector('.crumbs')!.getAttribute('aria-label')).toBe('… › Title b');
  expect(host.querySelector('.text')).toBeNull();
  expect(host.querySelector('.open-plan')).not.toBeNull();
 });
 it('shows one skeleton line while the read loads, then a retry when it fails but keeps Open in plan', () => {
  plan.status = 'loading'; plan.payload = null;
  render([card(1)]); openList(); expand(0);
  expect(host.querySelector('.skeleton')).not.toBeNull();
  expect(host.querySelector('.text')).toBeNull();
  host.remove(); plan.status = 'error';
  render([card(1)]); openList(); expand(0);
  expect(host.querySelector('.detail')!.textContent).toContain('Couldn’t load details · Retry');
  host.querySelector<HTMLButtonElement>('.detail .link:not(.open-plan)')!.click();
  expect(plan.retry).toHaveBeenCalled();
  host.querySelector<HTMLButtonElement>('.open-plan')!.click();
  expect(focus).toHaveBeenCalledWith('t', 'node-1', null);
 });
 it('opens a task in a subagent plan through the step that nests it, with no breadcrumb', () => {
  plan.payload = planRead(planNode('step-9', null, { child: { available: true, thread_id: 'child-1' } }));
  plan.children = { 'child-1': { status: 'ready', payload: planRead(planNode('node-1', null)) } };
  render([card(1)]); openList(); expand(0);
  expect(host.querySelector('.crumbs')).toBeNull();
  expect(host.querySelector('.text')).toBeNull();
  host.querySelector<HTMLButtonElement>('.open-plan')!.click();
  expect(focus).toHaveBeenCalledWith('t', 'node-1', 'step-9');
 });
 it('hides the link, and stops offering expansion, for a task no loaded plan holds', async () => {
  plan.payload = planRead(planNode('step-9', null, { child: { available: true, thread_id: 'child-1' } }));
  plan.children = { 'child-1': { status: 'ready', payload: planRead(planNode('other', null)) } };
  render([card(1)]); openList(); expand(0); await settle();
  expect(host.querySelector('.open-plan')).toBeNull();
  expect(rowsText()[0]).toContain('Original snapshot 1');
  expect(host.querySelectorAll('li > button.row')).toHaveLength(0);
  expect(host.querySelector('.detail')).toBeNull();
 });
 it('moves focus to the list toggle when an expanded row loses its button', async () => {
  plan.payload = planRead(planNode('step-9', null, { child: { available: true, thread_id: 'child-1' } }));
  plan.children = { 'child-1': { status: 'ready', payload: planRead(planNode('other', null)) } };
  render([card(1)]); openList();
  rowButton(0).focus(); expand(0); await settle();
  expect(document.activeElement).toBe(host.querySelector('.passive-toggle'));
 });
 it('reports a failed child plan read instead of hiding the link, and retries that read', () => {
  plan.payload = planRead(planNode('step-9', null, { child: { available: true, thread_id: 'child-1' } }));
  plan.children = { 'child-1': { status: 'error', payload: null } };
  render([card(1)]); openList(); expand(0);
  expect(host.querySelector('.detail')!.textContent).toContain('Couldn’t load details · Retry');
  expect(host.querySelector('.open-plan')).not.toBeNull();
  host.querySelector<HTMLButtonElement>('.detail .link:not(.open-plan)')!.click();
  expect(plan.retry).toHaveBeenCalled();
  expect(plan.childRetry).toHaveBeenCalledWith({ kind: 'child', id: 'child-1' });
 });
 it('reads a completed task the thread read leaves out as folded, keeping the row a button while it loads', async () => {
  plan.payload = planRead(planNode('step-9', null));
  let finish!: (value: unknown) => void;
  foldedRead.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  render([card(1)]); openList(); expand(0); await settle();
  expect(foldedRead).toHaveBeenCalledWith('tok', 'plan-1', 'node-1');
  expect(rowButton(0).getAttribute('aria-expanded')).toBe('true');
  expect(host.querySelector('.skeleton')).not.toBeNull();
  finish({ status: 'fresh', etag: '', payload: planNode('node-1', 'step-9', { description: 'What was done' }) });
  await settle();
  expect(host.querySelector('.text')!.textContent).toBe('What was done');
  expect(host.querySelector('.crumbs')!.textContent).toContain('Title step-9');
  host.querySelector<HTMLButtonElement>('.open-plan')!.click();
  expect(focus).toHaveBeenCalledWith('t', 'node-1', null);
 });
 it('offers a retry when the folded read fails, and loads the task on retry', async () => {
  plan.payload = planRead(planNode('step-9', null));
  foldedRead.mockResolvedValueOnce({ status: 'error', message: 'x' });
  render([card(1)]); openList(); expand(0); await settle();
  expect(host.querySelector('.detail')!.textContent).toContain('Couldn’t load details · Retry');
  foldedRead.mockResolvedValue({ status: 'fresh', etag: '', payload: planNode('node-1', null, { description: 'Back again' }) });
  host.querySelector<HTMLButtonElement>('.detail .link:not(.open-plan)')!.click();
  await settle();
  expect(host.querySelector('.text')!.textContent).toBe('Back again');
 });
 it('does not make a deleted task expandable', () => {
  render([{ ...card(1), node_exists: false }]); openList();
  expect(host.querySelector('li .row.deleted')).not.toBeNull();
  expect(host.querySelector('li > button.row')).toBeNull();
 });
});
