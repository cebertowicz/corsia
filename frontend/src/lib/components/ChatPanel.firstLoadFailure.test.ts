import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPanel from './ChatPanel.svelte';
import { withEmptyAttentionQueue } from '../__tests__/fixtures/attentionQueue';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages, reasoningEffort, threads } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { emit } from '$lib/ws/events';
import { _resetCheckpointsForTests } from '$lib/stores/checkpoints';
import { _resetSubagentHydrationForTests, subagentMeta } from '$lib/stores/subagents';
import { clearAllContextUsage } from '$lib/stores/contextUsageByThread';
import { coreSettings } from '$lib/stores/settings';

/**
 * When the first page of a thread fails, the canvas must not answer with
 * someone else's transcript. Rows that belong to the thread the user left are
 * a claim about the thread they opened.
 */

const originalFetch = globalThis.fetch;

let host: HTMLElement;
let component: Record<string, any> | null = null;

function mountChatPanel() {
	globalThis.fetch = withEmptyAttentionQueue(globalThis.fetch);
	component = mount(ChatPanel, {
		target: host,
		props: { onOpenLightbox: () => {} },
	});
}

function failMessagesFetch() {
	globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/messages?')) return { ok: false, status: 500, json: async () => ({}) };
		return { ok: true, status: 200, json: async () => ({ thread_kind: 'chat' }) };
	}) as unknown as typeof fetch;
}

beforeEach(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(), cancel: vi.fn(), commitStyles: vi.fn(),
			onfinish: null, playState: 'finished',
		})) as unknown as Element['animate'];
	}
	vi.spyOn(console, 'error').mockImplementation(() => {});
	token.set('tok');
	threads.set([{ id: 'thread-b', title: 'Thread B' } as never]);
	activeThreadId.set(null);
	messages.set([]);
	subagentMeta.set(new Map());
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	_resetSubagentHydrationForTests();
	clearAllContextUsage();
	coreSettings.set(null);
	reasoningEffort.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	messages.set([]);
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
	_resetCheckpointsForTests();
	coreSettings.set(null);
	globalThis.fetch = originalFetch;
	activeThreadId.set(null);
});

describe('ChatPanel first-page failure', () => {
	it('drops the previous thread rows instead of passing them off as this one', async () => {
		messages.set([
			{
				id: 'a1', thread_id: 'thread-a', role: 'assistant',
				content: 'answer from the thread the user left',
				created_at: '2026-08-01T00:00:00Z',
			},
		] as never);
		failMessagesFetch();

		mountChatPanel();
		flushSync();
		await (component as Record<string, any>).selectThread('thread-b');
		flushSync();

		expect(get(messages)).toEqual([]);
		expect(host.textContent).toContain("Couldn't load this thread");
		expect(host.querySelector('.empty-chat')).toBeNull();
	});

	it('refreshes a terminal summary for the open transcript without reselecting', async () => {
		activeThreadId.set('thread-b');
		coreSettings.set({ features: { focus_mode: true } } as never);
		let messageLoads = 0;
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/messages?')) {
				messageLoads += 1;
				const terminal = messageLoads > 1;
				return {
					ok: true, status: 200,
					json: async () => ({
						messages: [{
							id: 'work', thread_id: 'thread-b', role: 'assistant', content: '',
							metadata: { turn_id: 'turn-2', tool_calls: [{ id: 'call-1' }] },
							created_at: '2026-09-30T12:00:00Z', activity_weight: 1,
						}],
						has_more: false, oldest_id: 'work',
						turn_summaries: {
							'turn-2': {
								started_at: '2026-09-30T12:00:00Z',
								ended_at: terminal ? '2026-09-30T12:04:00Z' : null,
								status: terminal ? 'completed' : 'running', activity_count: 1,
							},
						},
					}),
				};
			}
			return { ok: true, status: 200, json: async () => ({}) };
		}) as unknown as typeof fetch;
		mountChatPanel();
		flushSync();
		const messageCalls = () => vi.mocked(globalThis.fetch).mock.calls
			.filter(([input]) => String(input).includes('/messages?')).length;

		emit('turn_activity', { thread_id: 'thread-b', turn_id: 'turn-2', turn_order: 2 });
		await vi.waitFor(() => expect(messageCalls()).toBe(1));
		await vi.waitFor(() => expect(host.textContent).toContain('Worked'));
		emit('turn_complete', { thread_id: 'thread-b', turn_id: 'turn-2', turn_order: 2 });
		await vi.waitFor(() => expect(messageCalls()).toBe(2));
		await vi.waitFor(() => expect(host.textContent).toContain('Worked · 1 step · 4 min'));
		emit('error', {
			thread_id: 'thread-b', turn_id: 'turn-3', turn_order: 3,
			error_type: 'stop_failed',
		});
		await Promise.resolve();
		expect(messageCalls()).toBe(2);
	});

	it('keeps completed labels for already-loaded older rows through a same-thread refresh', async () => {
		activeThreadId.set('thread-b');
		coreSettings.set({ features: { focus_mode: true } } as never);
		const summary = (turnId: string) => ({
			[turnId]: {
				started_at: '2026-09-30T12:00:00Z', ended_at: '2026-09-30T12:04:00Z',
				status: 'completed', activity_count: 1,
			},
		});
		let messageLoads = 0;
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			if (!String(input).includes('/messages?')) return { ok: true, status: 200, json: async () => ({}) };
			messageLoads += 1;
			const older = messageLoads === 1;
			const turnId = older ? 'older-turn' : 'new-turn';
			return { ok: true, status: 200, json: async () => ({
				messages: [{ id: older ? 'older-work' : 'new-work', thread_id: 'thread-b', role: 'assistant', content: '',
					metadata: { turn_id: turnId, tool_calls: [{ id: `${turnId}-call` }] },
					created_at: older ? '2026-09-30T12:00:00Z' : '2026-09-30T12:05:00Z', activity_weight: 1 }],
				has_more: false, oldest_id: older ? 'older-work' : 'new-work', turn_summaries: summary(turnId),
			}) };
		});
		globalThis.fetch = fetchMock as typeof fetch;
		mountChatPanel();
		flushSync();

		await (component as Record<string, any>).fetchThreadMessages('thread-b');
		await vi.waitFor(() => expect(host.textContent).toContain('Worked · 1 step · 4 min'));
		await (component as Record<string, any>).fetchThreadMessages('thread-b');
		await vi.waitFor(() => expect(host.textContent).toContain('Worked · 2 steps · 8 min'));
	});

	it('aborts the first page of the thread the user left, without an error state', async () => {
		threads.set([{ id: 'thread-a', title: 'Thread A' }, { id: 'thread-b', title: 'Thread B' }] as never);
		const signals = new Map<string, AbortSignal>();
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.includes('/thread-a/messages?')) {
				const signal = init?.signal as AbortSignal;
				signals.set('thread-a', signal);
				// A slow link: nothing arrives until the read is abandoned.
				return new Promise((_, reject) => signal.addEventListener('abort', () => (
					reject(new DOMException('The operation was aborted.', 'AbortError'))
				)));
			}
			if (url.includes('/thread-b/messages?')) {
				signals.set('thread-b', init?.signal as AbortSignal);
				return { ok: true, status: 200, json: async () => ({
					messages: [{ id: 'b1', thread_id: 'thread-b', role: 'user', content: 'hello b', created_at: '2026-08-01T00:00:00Z' }],
					has_more: false, oldest_id: 'b1', turn_summaries: {},
				}) };
			}
			return { ok: true, status: 200, json: async () => ({ thread_kind: 'chat' }) };
		}) as unknown as typeof fetch;

		mountChatPanel();
		flushSync();
		const leaving = (component as Record<string, any>).selectThread('thread-a');
		await vi.waitFor(() => expect(signals.has('thread-a')).toBe(true));
		await (component as Record<string, any>).selectThread('thread-b');
		await leaving;
		flushSync();

		expect(signals.get('thread-a')?.aborted).toBe(true);
		expect(signals.get('thread-b')?.aborted).toBe(false);
		expect(host.textContent).toContain('hello b');
		expect(host.textContent).not.toContain("Couldn't load this thread");
		expect(vi.mocked(console.error).mock.calls.some(([text]) => String(text).includes('thread-a'))).toBe(false);
	});

	it('keeps this thread rows when a refresh of the SAME thread fails', async () => {
		messages.set([
			{
				id: 'b1', thread_id: 'thread-b', role: 'assistant',
				content: 'answer already on screen', created_at: '2026-08-01T00:00:00Z',
			},
		] as never);
		activeThreadId.set('thread-b');
		failMessagesFetch();

		mountChatPanel();
		flushSync();
		await (component as Record<string, any>).fetchThreadMessages('thread-b');
		flushSync();

		expect(get(messages).map((m) => m.id)).toEqual(['b1']);
		expect(host.textContent).toContain("Couldn't load this thread");
	});
});
