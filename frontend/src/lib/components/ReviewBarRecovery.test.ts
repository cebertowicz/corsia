import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ReviewBar from './ReviewBar.svelte';
import type { ReviewBarRead } from '$lib/api/reviewBar';
import { createReviewBarState } from '$lib/reviewBarState.svelte';

const api = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
const ws = vi.hoisted(() => ({ listeners: new Map<string, () => void>(), on: vi.fn((event: string, handler: () => void) => { ws.listeners.set(event, handler); return () => ws.listeners.delete(event); }) }));
vi.mock('$lib/api/reviewBar', async (original) => ({ ...(await original<object>()), ...api }));
vi.mock('$lib/ws', () => ({ on: ws.on }));

const empty: ReviewBarRead = { ask_raised: false, hold_ids: [], ask_reason: null, ask_raised_at: null, asker: null, other_askers: [], other_asks: [], other_request_count: 0, round_feedback_count: 0, round_feedback_counts: { comments: 0, suggestions: 0, edits: 0 } };
const ask: ReviewBarRead = { ...empty, ask_raised: true, hold_ids: ['hold'], ask_reason: 'Please review', ask_raised_at: '2026-11-18T18:56:00Z', asker: { id: 'agent', name: 'Agent' } };
let host: HTMLElement;
let component: Record<string, unknown> | null;
let visibility: 'visible' | 'hidden';
const show = () => host.textContent?.includes('Approve');
async function settle() { await Promise.resolve(); await tick(); flushSync(); }
function visible(state: 'visible' | 'hidden') { visibility = state; document.dispatchEvent(new Event('visibilitychange')); }
function render() { component = mount(ReviewBar, { target: host, props: { artifactId: 'doc' } }); flushSync(); }

beforeEach(() => {
 vi.useFakeTimers(); visibility = 'visible';
 vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
 host = document.createElement('div'); document.body.appendChild(host); component = null;
 api.fetchReviewBar.mockReset(); ws.listeners.clear(); ws.on.mockClear();
});
afterEach(() => { if (component) unmount(component); host.remove(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe('review bar recovery', () => {
 it('reloads a raised ask after authenticated reconnect', async () => {
  api.fetchReviewBar.mockResolvedValueOnce(empty).mockResolvedValueOnce(ask);
  render(); await settle(); expect(show()).toBeFalsy();
  ws.listeners.get('connected')?.(); await vi.advanceTimersByTimeAsync(150); await settle();
  expect(show()).toBeTruthy();
 });
 it.each(['focus', 'visibility', 'reconnect'] as const)('recovers a failed read on %s', async (event) => {
  api.fetchReviewBar.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(ask);
  render(); await settle(); expect(show()).toBeFalsy();
  expect(api.fetchReviewBar).toHaveBeenCalledTimes(1);
  if (event === 'focus') window.dispatchEvent(new Event('focus'));
  else if (event === 'visibility') { visible('hidden'); visible('visible'); }
  else ws.listeners.get('connected')?.();
  await vi.advanceTimersByTimeAsync(150); await settle(); expect(show()).toBeTruthy();
  expect(api.fetchReviewBar).toHaveBeenCalledTimes(2);
  unmount(component!); component = null;
  window.dispatchEvent(new Event('focus')); visible('hidden'); visible('visible'); ws.listeners.get('connected')?.();
  await vi.advanceTimersByTimeAsync(150); expect(api.fetchReviewBar).toHaveBeenCalledTimes(2);
 });
 it('times out a hanging read and retries while focused and connected', async () => {
  const actual = await vi.importActual<typeof import('$lib/api/reviewBar')>('$lib/api/reviewBar');
  const fetchMock = vi.spyOn(globalThis, 'fetch');
  fetchMock.mockImplementationOnce((_input, init) => new Promise((_resolve, reject) => {
   init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  })).mockResolvedValueOnce(new Response(JSON.stringify(ask), { status: 200 }));
  api.fetchReviewBar.mockImplementation(actual.fetchReviewBar);
  render(); await settle();
  await vi.advanceTimersByTimeAsync(12_000); await settle(); expect(show()).toBeFalsy();
  await vi.advanceTimersByTimeAsync(5_000); await settle(); expect(show()).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledTimes(2);
 });
 it('retries a failed read', async () => {
  api.fetchReviewBar.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(ask);
  render(); await settle(); await vi.advanceTimersByTimeAsync(5_000); await settle();
  expect(show()).toBeTruthy(); expect(api.fetchReviewBar).toHaveBeenCalledTimes(2);
 });
 it('cancels a pending retry on deactivate', async () => {
  api.fetchReviewBar.mockRejectedValue(new Error('offline'));
  const bar = createReviewBarState(); bar.setTarget('doc'); await bar.load(); bar.deactivate();
  await vi.advanceTimersByTimeAsync(65_000); expect(api.fetchReviewBar).toHaveBeenCalledTimes(1);
 });
 it('stops after three retries', async () => {
  api.fetchReviewBar.mockRejectedValue(new Error('offline'));
  const bar = createReviewBarState(); bar.setTarget('doc'); await bar.load();
  for (const [delay, calls] of [[5_000, 2], [15_000, 3], [45_000, 4]]) {
   await vi.advanceTimersByTimeAsync(delay - 1); expect(api.fetchReviewBar).toHaveBeenCalledTimes(calls - 1);
   await vi.advanceTimersByTimeAsync(1); expect(api.fetchReviewBar).toHaveBeenCalledTimes(calls);
  }
  await vi.advanceTimersByTimeAsync(120_000); expect(api.fetchReviewBar).toHaveBeenCalledTimes(4);
  bar.deactivate();
 });
 it('reloads on visibility even when the previous read never settles', async () => {
  let finish!: (value: ReviewBarRead) => void;
  api.fetchReviewBar.mockReturnValueOnce(new Promise<ReviewBarRead>((resolve) => { finish = resolve; })).mockResolvedValueOnce(ask);
  render(); await settle(); visible('hidden'); visible('visible'); await settle();
  expect(show()).toBeTruthy(); finish(empty); await settle(); expect(show()).toBeTruthy();
 });
 it('does not retry an old failed read after switching targets', async () => {
  let fail!: (error: Error) => void;
  api.fetchReviewBar.mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject; })).mockResolvedValueOnce(ask);
  const bar = createReviewBarState(); bar.setTarget('old'); void bar.load();
  bar.setTarget('new'); await bar.load(); fail(new Error('late')); await settle();
  await vi.advanceTimersByTimeAsync(60000);
  expect(api.fetchReviewBar).toHaveBeenCalledTimes(2); expect(bar.read).toEqual(ask);
  bar.deactivate();
 });
 it('recovers a person step on return to the visible page', async () => {
  api.fetchReviewBar.mockResolvedValueOnce(empty).mockResolvedValueOnce({ ...empty, person_step: { node_id: 'n', node_title: 'Read', node_revision: 1, plan_id: 'p', owner_thread_id: 'a', owner_name: 'Agent' } });
  render(); await settle(); visible('hidden'); visible('visible'); await settle();
  expect(host.textContent).toContain('Done');
 });
});
