import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPanel from './ChatPanel.svelte';
import { withEmptyAttentionQueue } from '../__tests__/fixtures/attentionQueue';
import { token } from '$lib/stores/auth';
import {
	_resetThreadModelTruthForTests,
	acceptThreadModelTruth,
	activeModelId,
	models,
	reconcileThreadModelSnapshots,
	setActiveModel,
	threadModelFor,
} from '$lib/stores/models';
import {
	activeThreadId,
	messages,
	reasoningEffort,
	threads,
} from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { _resetSubagentHydrationForTests, subagentMeta } from '$lib/stores/subagents';
import {
	_resetCheckpointsForTests,
	checkpointsByThread,
} from '$lib/stores/checkpoints';
import { clearAllContextUsage } from '$lib/stores/contextUsageByThread';

const originalFetch = globalThis.fetch;

let host: HTMLElement;
let component: Record<string, any> | null = null;

function mockMessagesFetch() {
	globalThis.fetch = vi.fn(async () => ({
		ok: true,
		json: async () => ({ messages: [], has_more: false, next_cursor: null }),
	})) as unknown as typeof fetch;
}

beforeEach(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
			commitStyles: vi.fn(),
			onfinish: null,
			playState: 'finished',
		})) as unknown as Element['animate'];
	}
	token.set('tok');
	threads.set([]);
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
	activeThreadId.set(null);
	messages.set([]);
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	clearAllContextUsage();
	reasoningEffort.set(null);
	mockMessagesFetch();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	_resetCheckpointsForTests();
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
	globalThis.fetch = originalFetch;
});

function renderChatPanel(): Record<string, any> {
	globalThis.fetch = withEmptyAttentionQueue(globalThis.fetch);
	component = mount(ChatPanel, {
		target: host,
		props: {
			onOpenLightbox: () => {},
		},
	});
	flushSync();
	return component!;
}

describe('ChatPanel session model hydration', () => {
	it('never treats selecting or switching threads as warm intent', async () => {
		models.set([{
			id: 'omlx/local-ready',
			name: 'Local Ready',
			size: '',
			backend: 'network',
			speed: '',
			status: 'ready',
		}]);
		threads.set(['thread-a', 'thread-b', 'thread-c'].map((id) => ({
			id,
			user_id: 'u-test',
			profile: 'personal',
			title: id,
			model: 'omlx/local-ready',
			created_at: '2026-08-04T00:00:00Z',
			updated_at: '2026-08-04T00:00:00Z',
		})));
		const requests: string[] = [];
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			requests.push(String(input));
			return {
				ok: true,
				json: async () => ({ messages: [], has_more: false, next_cursor: null }),
			} as Response;
		}) as typeof fetch;
		const panel = renderChatPanel();

		await panel.selectThread('thread-a');
		await panel.selectThread('thread-b');
		await panel.selectThread('thread-c');
		await tick();

		expect(requests.filter((url) => url.includes('/warm?'))).toEqual([]);
	});

	it('deduplicates composer warm intent per session and model across A-B-C-A', async () => {
		models.set([{
			id: 'omlx/local-ready',
			name: 'Local Ready',
			size: '',
			backend: 'network',
			speed: '',
			status: 'ready',
		}]);
		activeModelId.set('omlx/local-ready');
		threads.set(['thread-a', 'thread-b', 'thread-c'].map((id) => ({
			id, user_id: 'u-test', profile: 'personal', title: id, model: 'omlx/local-ready',
			created_at: '2026-06-17T00:00:00', updated_at: '2026-06-17T00:00:00',
		})));
		const requests: string[] = [];
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			requests.push(String(input));
			return { ok: true, json: async () => ({}) } as Response;
		}) as typeof fetch;
		const panel = renderChatPanel();

		for (const threadId of ['thread-a', 'thread-b', 'thread-c', 'thread-a']) {
			activeThreadId.set(threadId);
			await panel.triggerWarm();
			await panel.triggerWarm();
		}

		expect(requests.filter((url) => url.includes('/warm?'))).toEqual([
			expect.stringContaining('/threads/thread-a/warm?'),
			expect.stringContaining('/threads/thread-b/warm?'),
			expect.stringContaining('/threads/thread-c/warm?'),
		]);
	});

	it('keeps a newer delivery revision when a stale history response arrives', async () => {
		activeThreadId.set('thread-revision');
		messages.set([{
			id: 'canonical-input-1', thread_id: 'thread-revision', role: 'user',
			content: 'do not regress this delivery',
			metadata: { input_id: 'input-1' },
			created_at: '2026-07-22T12:00:00Z',
			delivery_state: 'delivered', delivery_error: null, delivery_revision: 2,
		}]);
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('/api/threads/thread-revision/messages')) {
				return {
					ok: true,
					json: async () => ({
						messages: [{
							id: 'canonical-input-1', thread_id: 'thread-revision', role: 'user',
							content: 'do not regress this delivery',
							metadata: { input_id: 'input-1' },
							created_at: '2026-07-22T12:00:00Z',
							delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
						}],
						has_more: false,
						oldest_id: 'canonical-input-1',
					}),
				} as Response;
			}
			return { ok: true, json: async () => ({ leases: [] }) } as Response;
		}) as typeof fetch;
		const panel = renderChatPanel();

		await panel.fetchThreadMessages('thread-revision');

		expect(get(messages)).toEqual([expect.objectContaining({
			id: 'canonical-input-1',
			delivery_state: 'delivered',
			delivery_revision: 2,
		})]);
	});

	it('keeps a visibility refetch union chronologically ordered with the newest durable row last', async () => {
		const page = Array.from({ length: 50 }, (_, offset) => {
			const index = offset + 52;
			return {
				id: `message-${String(index).padStart(3, '0')}`,
				thread_id: 'thread-visibility', role: 'assistant', content: `message ${index}`,
				metadata: {}, created_at: new Date(Date.UTC(2026, 6, 27, 0, index)).toISOString(),
				delivery_state: index === 101 ? 'failed' : undefined,
				delivery_revision: index === 101 ? 3 : undefined,
			};
		});
		activeThreadId.set('thread-visibility');
		messages.set([
			{
				id: 'message-001', thread_id: 'thread-visibility', role: 'assistant', content: 'older one',
				metadata: {}, created_at: new Date(Date.UTC(2026, 6, 27, 0, 1)).toISOString(),
			},
			{
				id: 'message-002', thread_id: 'thread-visibility', role: 'assistant', content: 'older two',
				metadata: {}, created_at: new Date(Date.UTC(2026, 6, 27, 0, 2)).toISOString(),
			},
			{
				id: 'message-101', thread_id: 'thread-visibility', role: 'assistant', content: 'live newest',
				metadata: {}, created_at: new Date(Date.UTC(2026, 6, 27, 0, 101)).toISOString(),
				delivery_state: 'delivered', delivery_revision: 4,
			},
		]);
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('/api/threads/thread-visibility/messages')) {
				return {
					ok: true,
					json: async () => ({ messages: page, has_more: true, oldest_id: 'message-052' }),
				} as Response;
			}
			// An open-time read with no `items` fails and retries on a timer that outlives this test.
			if (String(input).includes('/api/open-time')) {
				return { ok: true, json: async () => ({ items: {} }) } as Response;
			}
			return { ok: true, json: async () => ({ leases: [] }) } as Response;
		}) as typeof fetch;
		vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
		renderChatPanel();

		document.dispatchEvent(new Event('visibilitychange'));
		await vi.waitFor(() => expect(get(messages)).toHaveLength(52));

		expect(get(messages).map((row) => row.id)).toEqual([
			'message-001',
			'message-002',
			...Array.from({ length: 50 }, (_, offset) => `message-${String(offset + 52).padStart(3, '0')}`),
		]);
		expect(get(messages).at(-1)).toMatchObject({
			id: 'message-101', delivery_state: 'delivered', delivery_revision: 4,
		});
	});

	it('retains a live canonical row that a stale history response does not contain', async () => {
		activeThreadId.set('thread-revision');
		messages.set([{
			id: 'canonical-live-input', thread_id: 'thread-revision', role: 'user',
			content: 'arrived after the request started',
			metadata: { input_id: 'input-live' },
			created_at: '2026-07-22T12:01:00Z',
			delivery_state: 'delivered', delivery_error: null, delivery_revision: 2,
		}]);
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('/api/threads/thread-revision/messages')) {
				return {
					ok: true,
					json: async () => ({
						messages: [{
							id: 'older-history-row', thread_id: 'thread-revision', role: 'assistant',
							content: 'history from before the live input', metadata: {},
							created_at: '2026-07-22T12:00:00Z',
						}],
						has_more: false,
						oldest_id: 'older-history-row',
					}),
				} as Response;
			}
			return { ok: true, json: async () => ({ leases: [] }) } as Response;
		}) as typeof fetch;
		const panel = renderChatPanel();

		await panel.fetchThreadMessages('thread-revision');

		expect(get(messages)).toEqual(expect.arrayContaining([
			expect.objectContaining({ id: 'older-history-row' }),
			expect.objectContaining({
				id: 'canonical-live-input',
				delivery_state: 'delivered',
				delivery_revision: 2,
			}),
		]));
		expect(get(messages)).toHaveLength(2);
	});

	it('does not restore a stale first-page response after New Chat clears the active session', async () => {
		let resolveResponse!: (response: Response) => void;
		const deferredResponse = new Promise<Response>((resolve) => {
			resolveResponse = resolve;
		});
		const fetchMock = vi.fn((input: RequestInfo | URL) => {
			if (String(input).includes('/api/threads/thread-stale/messages')) {
				return deferredResponse;
			}
			return Promise.resolve({
				ok: true,
				json: async () => ({ leases: [] }),
			} as Response);
		});
		globalThis.fetch = fetchMock as typeof fetch;
		activeThreadId.set('thread-stale');
		const panel = renderChatPanel();

		const loading = panel.fetchThreadMessages('thread-stale');
		await vi.waitFor(() => expect(fetchMock.mock.calls.some(([input]) =>
			String(input).includes('/api/threads/thread-stale/messages'),
		)).toBe(true));
		activeThreadId.set(null);
		messages.set([]);
		flushSync();
		resolveResponse({
			ok: true,
			json: async () => ({
				messages: [{
					id: 'stale-thread-row', thread_id: 'thread-stale', role: 'user',
					content: 'must not return after New Chat', metadata: {},
					created_at: '2026-07-22T12:00:00Z',
				}],
				has_more: false,
			}),
		} as Response);
		await loading;

		expect(get(messages)).toEqual([]);
	});

	it('keeps a live WebSocket row when a deferred first-page fetch fails', async () => {
		let rejectResponse!: (reason?: unknown) => void;
		const deferredResponse = new Promise<Response>((_resolve, reject) => {
			rejectResponse = reject;
		});
		const fetchMock = vi.fn((input: RequestInfo | URL) => {
			if (String(input).includes('/api/threads/thread-live/messages')) {
				return deferredResponse;
			}
			return Promise.resolve({
				ok: true,
				json: async () => ({ leases: [] }),
			} as Response);
		});
		globalThis.fetch = fetchMock as typeof fetch;
		vi.spyOn(console, 'error').mockImplementation(() => {});
		activeThreadId.set('thread-live');
		const panel = renderChatPanel();

		const loading = panel.fetchThreadMessages('thread-live');
		await vi.waitFor(() => expect(fetchMock.mock.calls.some(([input]) =>
			String(input).includes('/api/threads/thread-live/messages'),
		)).toBe(true));
		messages.set([{
			id: 'live-ws-row', thread_id: 'thread-live', role: 'user',
			content: 'arrived over WebSocket', metadata: { input_id: 'live-input' },
			created_at: '2026-07-22T12:01:00Z',
			delivery_state: 'delivered', delivery_revision: 2,
		}]);
		rejectResponse(new Error('network unavailable'));
		await loading;

		expect(get(messages)).toEqual([expect.objectContaining({
			id: 'live-ws-row',
			delivery_state: 'delivered',
			delivery_revision: 2,
		})]);
	});

	it('rehydrates checkpoint history when the active thread changes', async () => {
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/checkpoints?')) {
				return {
					ok: true,
					json: async () => ({
						checkpoints: [{
							id: 'checkpoint-1', trigger: 'model_switch', mode: 'warm',
							status: 'running', writerModel: 'native/source', error: null,
							startedAt: '2026-07-21T08:00:00Z',
						}],
					}),
				} as Response;
			}
			return {
				ok: true,
				json: async () => ({ messages: [], has_more: false, next_cursor: null }),
			} as Response;
		}) as typeof fetch;
		renderChatPanel();
		activeThreadId.set('thread-checkpoint');
		flushSync();

		await vi.waitFor(() => {
			expect(get(checkpointsByThread).get('thread-checkpoint')).toMatchObject({
				checkpoints: [{ id: 'checkpoint-1', status: 'running' }],
				loading: false,
				error: null,
			});
		});
	});

	it('keeps the conductor model accepted from its own server lookup when the session list omits it', async () => {
		_resetThreadModelTruthForTests();
		setActiveModel('omlx/local-last-choice');
		acceptThreadModelTruth('hidden-conductor', 'codex/gpt-5-codex', '2026-06-18T00:00:00');
		const panel = renderChatPanel();

		await panel.selectThread('hidden-conductor');

		expect(get(activeThreadId)).toBe('hidden-conductor');
		expect(get(activeModelId)).toBe('codex/gpt-5-codex');
	});

	it('binds the accepted model of a thread hidden from the list after a list reload re-asserted the previous thread', async () => {
		_resetThreadModelTruthForTests();
		const previous = {
			id: 'visible-chat',
			user_id: 'u-test',
			profile: 'personal',
			title: 'Visible Chat',
			model: 'claude-code/opus',
			created_at: '2026-06-17T00:00:00',
			updated_at: '2026-06-17T00:00:00',
		};
		const panel = renderChatPanel();
		threads.set(reconcileThreadModelSnapshots([previous]));
		await panel.selectThread('visible-chat');
		// The conductor's own read lands while the previous thread is still open.
		acceptThreadModelTruth('hidden-conductor', 'codex/gpt-5-codex', '2026-06-18T00:00:00');
		// Opening a thread the list does not hold reloads the list first.
		threads.set(reconcileThreadModelSnapshots([previous]));
		expect(get(activeModelId)).toBe('claude-code/opus');

		await panel.selectThread('hidden-conductor');

		expect(get(activeModelId)).toBe('codex/gpt-5-codex');
		_resetThreadModelTruthForTests();
	});

	it('keeps prehydrated conductor reasoning effort when the opened session is hidden from the session list', async () => {
		reasoningEffort.set('max');
		const panel = renderChatPanel();

		await panel.selectThread('hidden-conductor');

		expect(get(activeThreadId)).toBe('hidden-conductor');
		expect(get(reasoningEffort)).toBe('max');
	});

	it('uses a visible session row model when one is available', async () => {
		setActiveModel('omlx/local-last-choice');
		threads.set([{
			id: 'visible-chat',
			user_id: 'u-test',
			profile: 'personal',
			title: 'Visible Chat',
			model: 'codex/gpt-5-codex',
			created_at: '2026-06-17T00:00:00',
			updated_at: '2026-06-17T00:00:00',
		}]);
		const panel = renderChatPanel();

		await panel.selectThread('visible-chat');

		expect(get(activeModelId)).toBe('codex/gpt-5-codex');
	});

	it('keeps the selected Codex model when an older Opus thread finishes loading late', async () => {
		activeModelId.set(null);
		let resolveOpus!: (response: Response) => void;
		let resolveSol!: (response: Response) => void;
		const opusPage = new Promise<Response>((resolve) => { resolveOpus = resolve; });
		const solPage = new Promise<Response>((resolve) => { resolveSol = resolve; });
		const fetchMock = vi.fn((input: RequestInfo | URL) => {
			if (String(input).includes('/api/threads/opus-thread/messages')) return opusPage;
			if (String(input).includes('/api/threads/sol-thread/messages')) return solPage;
			return Promise.resolve({
				ok: true,
				json: async () => ({ messages: [], has_more: false, next_cursor: null }),
			} as Response);
		});
		globalThis.fetch = fetchMock as typeof fetch;
		threads.set([
			{ id: 'opus-thread', user_id: 'u-test', profile: 'personal', title: 'Opus', model: 'claude-code/opus', created_at: '2026-06-17T00:00:00', updated_at: '2026-06-17T00:00:00' },
			{ id: 'sol-thread', user_id: 'u-test', profile: 'personal', title: 'Sol', model: 'codex/gpt-5.6-sol', created_at: '2026-06-17T00:00:00', updated_at: '2026-06-17T00:00:00' },
		]);
		const panel = renderChatPanel();
		const openingOpus = panel.selectThread('opus-thread');
		await vi.waitFor(() => expect(fetchMock.mock.calls.some(([input]) =>
			String(input).includes('/api/threads/opus-thread/messages'),
		)).toBe(true));
		expect(get(activeModelId)).toBe('claude-code/opus');
		const openingSol = panel.selectThread('sol-thread');
		await vi.waitFor(() => expect(fetchMock.mock.calls.some(([input]) =>
			String(input).includes('/api/threads/sol-thread/messages'),
		)).toBe(true));
		expect(get(activeModelId)).toBe('codex/gpt-5.6-sol');
		resolveSol({
			ok: true,
			json: async () => ({ messages: [], has_more: false, next_cursor: null }),
		} as Response);
		await openingSol;

		resolveOpus({
			ok: true,
			json: async () => ({ messages: [], has_more: false, next_cursor: null }),
		} as Response);
		await openingOpus;
		expect(get(activeThreadId)).toBe('sol-thread');
		expect(get(activeModelId)).toBe('codex/gpt-5.6-sol');
	});

	it('clears a preceding model when an unlisted thread cannot be hydrated', async () => {
		_resetThreadModelTruthForTests();
		threads.set([{ id: 'opus-thread', user_id: 'u-test', profile: 'personal', title: 'Opus', model: 'claude-code/opus', created_at: '2026-06-17T00:00:00', updated_at: '2026-06-17T00:00:00' }]);
		const panel = renderChatPanel();
		await panel.selectThread('opus-thread');
		expect(get(activeModelId)).toBe('claude-code/opus');
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => ({
			ok: !String(input).includes('/delegation/threads/unknown-thread/status'),
			json: async () => ({ messages: [], has_more: false, next_cursor: null }),
		} as Response)) as typeof fetch;

		await panel.selectThread('unknown-thread');

		expect(get(activeThreadId)).toBe('unknown-thread');
		expect(get(activeModelId)).toBeNull();
	});

	it('uses a hidden subagent model read from the server instead of the preceding thread model', async () => {
		activeModelId.set('claude-code/opus');
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => ({
			ok: true,
			json: async () => String(input).includes('/delegation/threads/sol-child/status')
				? { thread_kind: 'subagent', thread_id: 'sol-child', title: 'Sol child', driver: 'codex', model: 'codex/gpt-5.6-sol', busy: false, turn_active: false }
				: { messages: [], has_more: false, next_cursor: null },
		} as Response)) as typeof fetch;
		const panel = renderChatPanel();

		await panel.selectThread('sol-child');

		expect(threadModelFor('sol-child')).toBe('codex/gpt-5.6-sol');
		expect(get(activeModelId)).toBe('codex/gpt-5.6-sol');
	});

	it('does not invent the last chosen model for a visible thread with no model', async () => {
		setActiveModel('claude-code/opus');
		threads.set([{ id: 'unbound', user_id: 'u-test', profile: 'personal', title: 'Unbound', model: null, created_at: '2026-06-17T00:00:00', updated_at: '2026-06-17T00:00:00' }]);
		const panel = renderChatPanel();

		await panel.selectThread('unbound');

		expect(get(activeModelId)).toBeNull();
	});

	it('binds the saved model when another navigation path changes the active thread', () => {
		threads.set([
			{ id: 'opus', user_id: 'u-test', profile: 'personal', title: 'Opus', model: 'claude-code/opus', created_at: '2026-06-17T00:00:00', updated_at: '2026-06-17T00:00:00' },
			{ id: 'sol', user_id: 'u-test', profile: 'personal', title: 'Sol', model: 'codex/gpt-5.6-sol', created_at: '2026-06-17T00:00:00', updated_at: '2026-06-17T00:00:00' },
		]);
		renderChatPanel();
		activeThreadId.set('opus');
		flushSync();
		expect(get(activeModelId)).toBe('claude-code/opus');
		activeThreadId.set('sol');
		flushSync();
		expect(get(activeModelId)).toBe('codex/gpt-5.6-sol');
	});
});

describe('ChatPanel host-access scope', () => {
	it('keeps an older lease refresh from leaking into a newly selected thread', async () => {
		let resolveFirst!: (response: Response) => void;
		const firstResponse = new Promise<Response>((resolve) => {
			resolveFirst = resolve;
		});
		let leaseFetches = 0;
		const lease = (threadId: string, title: string) => ({
			id: `lease-${threadId}`,
			root_thread_id: threadId,
			root_thread_title: title,
			state: 'active',
			purpose: threadId === 'thread-1'
				? 'Inspect the alpha host.'
				: 'Repair the beta host.',
			requested_duration_seconds: 60,
			application_hash: `hash-${threadId}`,
			revision: 1,
			decision_capability: `decision-${threadId}`,
			reviewer: {
				model: `native/reviewer-${threadId}`,
				display_name: threadId === 'thread-1' ? 'Reviewer Alpha' : 'Reviewer Beta',
			},
			expires_at: '2099-07-18T16:06:00+00:00',
		});
		globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).startsWith('/api/host-access/leases?')) {
				leaseFetches += 1;
				if (leaseFetches === 1) return firstResponse;
				return {
					ok: true,
					json: async () => ({ leases: [lease('thread-2', 'Thread two')] }),
				} as Response;
			}
			return {
				ok: true,
				json: async () => ({ messages: [], has_more: false, next_cursor: null }),
			} as Response;
		}) as typeof fetch;

		activeThreadId.set('thread-1');
		renderChatPanel();
		await vi.waitFor(() => expect(leaseFetches).toBe(1));

		activeThreadId.set('thread-2');
		flushSync();
		await vi.waitFor(() => {
			expect(host.querySelector('[aria-label="Host access"]')).toBeTruthy();
		});
		expect(host.textContent).not.toContain('Thread two');

		resolveFirst({
			ok: true,
			json: async () => ({ leases: [lease('thread-1', 'Thread one')] }),
		} as Response);
		await tick();
		await tick();

		expect(host.querySelector('[aria-label="Host access"]')).toBeTruthy();
		expect(host.textContent).not.toContain('Thread two');
		expect(host.textContent).not.toContain('Thread one');

		const trigger = host.querySelector<HTMLButtonElement>('.active-chip [aria-label="Host access"]')!;
		trigger.click();
		flushSync();
		await tick();
		const dialog = document.querySelector('[role="dialog"]')!;
		expect(dialog).toBeTruthy();
		expect(dialog.textContent).toContain('Repair the beta host.');
		expect(dialog.textContent).toContain('Reviewer Beta');
		expect(dialog.textContent).not.toContain('Inspect the alpha host.');
		expect(dialog.textContent).not.toContain('Reviewer Alpha');
	});

	it('does not let an older thread decision clear the selected thread decision lock', async () => {
		let resolveFirstDecision!: (response: Response) => void;
		let resolveSecondDecision!: (response: Response) => void;
		const firstDecision = new Promise<Response>((resolve) => {
			resolveFirstDecision = resolve;
		});
		const secondDecision = new Promise<Response>((resolve) => {
			resolveSecondDecision = resolve;
		});
		const lease = (threadId: string, title: string) => ({
			id: `lease-${threadId}`,
			root_thread_id: threadId,
			root_thread_title: title,
			state: 'awaiting_user',
			purpose: `Approve work for ${title}.`,
			plan: ['Perform the bounded action'],
			requested_duration_seconds: 60,
			expected_resources: null,
			persistent_effects: null,
			rollback_plan: null,
			forbidden_effects: null,
			requesting_project_id: null,
			repo_policy_snapshot: [],
			repo_policy_hash: 'a'.repeat(64),
			requested_policy_exceptions: [],
			application_hash: `hash-${threadId}`,
			revision: 1,
			decision_capability: `decision-${threadId}`,
			reviewer_model: 'native/reviewer',
			reviewer: {
				model: 'native/reviewer',
				display_name: 'Reviewer',
				attestation: 'Bounded.',
			},
			application_review_result: { attestation: 'Bounded.' },
			expires_at: null,
			terminal_reason: null,
			created_at: '2026-07-20T23:00:00Z',
			updated_at: '2026-07-20T23:00:00Z',
		});
		globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/host-access/leases?')) {
				const threadId = get(activeThreadId)!;
				return Promise.resolve({
					ok: true,
					json: async () => ({
						leases: [lease(threadId, threadId === 'thread-1' ? 'Thread one' : 'Thread two')],
					}),
				} as Response);
			}
			if (init?.method === 'POST') {
				return url.includes('lease-thread-1') ? firstDecision : secondDecision;
			}
			return Promise.resolve({
				ok: true,
				json: async () => ({ messages: [], has_more: false, next_cursor: null }),
			} as Response);
		}) as typeof fetch;
		const approve = () => Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Approve');
		const openPendingDrawer = () => {
			host.querySelector<HTMLButtonElement>('[aria-label="Review temporary host access"]')!.click();
			flushSync();
		};

		activeThreadId.set('thread-1');
		renderChatPanel();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		openPendingDrawer();
		await vi.waitFor(() => expect(approve()).toBeTruthy());
		approve()!.click();

		activeThreadId.set('thread-2');
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Thread two'));
		openPendingDrawer();
		await vi.waitFor(() => expect(approve()).toBeTruthy());
		approve()!.click();
		await tick();
		expect(approve()?.disabled).toBe(true);

		resolveFirstDecision({ ok: true, json: async () => lease('thread-1', 'Thread one') } as Response);
		await new Promise((resolve) => setTimeout(resolve, 0));
		await tick();
		expect(approve()?.disabled).toBe(true);

		resolveSecondDecision({ ok: true, json: async () => lease('thread-2', 'Thread two') } as Response);
		await new Promise((resolve) => setTimeout(resolve, 0));
		await tick();
	});

	it('does not allow a conflicting decision after switching away and back', async () => {
		let resolveFirstDecision!: (response: Response) => void;
		const firstDecision = new Promise<Response>((resolve) => {
			resolveFirstDecision = resolve;
		});
		let decisionCount = 0;
		const lease = (threadId: string, title: string) => ({
			id: `lease-${threadId}`,
			root_thread_id: threadId,
			root_thread_title: title,
			state: 'awaiting_user',
			purpose: `Approve work for ${title}.`,
			plan: ['Perform the bounded action'],
			requested_duration_seconds: 60,
			expected_resources: null,
			persistent_effects: null,
			rollback_plan: null,
			forbidden_effects: null,
			requesting_project_id: null,
			repo_policy_snapshot: [],
			repo_policy_hash: 'a'.repeat(64),
			requested_policy_exceptions: [],
			application_hash: `hash-${threadId}`,
			revision: 1,
			decision_capability: `decision-${threadId}`,
			reviewer_model: 'native/reviewer',
			reviewer: {
				model: 'native/reviewer',
				display_name: 'Reviewer',
				attestation: 'Bounded.',
			},
			application_review_result: { attestation: 'Bounded.' },
			expires_at: null,
			terminal_reason: null,
			created_at: '2026-07-20T23:00:00Z',
			updated_at: '2026-07-20T23:00:00Z',
		});
		globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
			if (String(input).startsWith('/api/host-access/leases?')) {
				const threadId = get(activeThreadId)!;
				return Promise.resolve({
					ok: true,
					json: async () => ({
						leases: [lease(threadId, threadId === 'thread-a' ? 'Thread A' : 'Thread B')],
					}),
				} as Response);
			}
			if (init?.method === 'POST' && /\/api\/host-access\/leases\/[^/]+\/(approve|reject)$/.test(String(input))) {
				decisionCount += 1;
				return decisionCount === 1
					? firstDecision
					: Promise.resolve({ ok: true, json: async () => ({}) } as Response);
			}
			return Promise.resolve({
				ok: true,
				json: async () => ({ messages: [], has_more: false, next_cursor: null }),
			} as Response);
		}) as typeof fetch;
		const decisionButton = (label: string) => Array.from(
			document.querySelectorAll<HTMLButtonElement>('button'),
		).find((button) => button.textContent?.trim() === label);
		const openPendingDrawer = () => {
			host.querySelector<HTMLButtonElement>('[aria-label="Review temporary host access"]')!.click();
			flushSync();
		};

		activeThreadId.set('thread-a');
		renderChatPanel();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Review temporary host access"]')).toBeTruthy());
		openPendingDrawer();
		await vi.waitFor(() => expect(decisionButton('Approve')).toBeTruthy());
		decisionButton('Approve')!.click();
		await tick();
		expect(decisionCount).toBe(1);

		activeThreadId.set('thread-b');
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Thread B'));

		activeThreadId.set('thread-a');
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Thread A'));
		await tick();
		openPendingDrawer();
		await vi.waitFor(() => expect(decisionButton('Approve')).toBeTruthy());
		expect(decisionButton('Approve')?.disabled).toBe(true);
		expect(decisionButton('Reject')?.disabled).toBe(true);

		decisionButton('Reject')!.click();
		await tick();
		expect(decisionCount).toBe(1);

		resolveFirstDecision({ ok: true, json: async () => lease('thread-a', 'Thread A') } as Response);
		await new Promise((resolve) => setTimeout(resolve, 0));
		await tick();
	});
});
