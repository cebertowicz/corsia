import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '$lib/stores/threads';
import { threadActivity } from '$lib/stores/activity';
import {
	handleTurnActivity,
	handleTurnComplete,
	handleTurnInterrupted,
} from './handlersTurns';
import { handleWsError } from './handlersConnection';
import { handleMessage } from './handlers';
import { on } from './events';
import {
	_resetTurnStateForTests,
	applyLifecycle,
	getTurnState,
	requestInput,
	type TurnIdentity,
} from './turnState';
import { token } from '$lib/stores/auth';
import {
	_resetCheckpointsForTests,
	checkpointsByThread,
} from '$lib/stores/checkpoints';
import { clearAllContextUsage, getContextUsage, setContextUsage } from '../stores/contextUsageByThread';

const VIEWED: TurnIdentity = { turnId: 'turn-viewed', turnOrder: 1 };
const BG: TurnIdentity = { turnId: 'turn-bg', turnOrder: 2 };

function frame(
	payload: Record<string, unknown>,
	identity: TurnIdentity,
): Record<string, unknown> {
	return {
		...payload,
		turn_id: identity.turnId,
		turn_order: identity.turnOrder,
	};
}

// #576: a turn TERMINAL (interrupt / error) must clear the drawer active-spinner
// off-thread, and an off-thread error must surface unread. Before the fix
// turn_interrupted was ignored and error never cleared activity → stuck spinner.

beforeEach(() => {
	activeThreadId.set('viewed');
	messages.set([]);
	clearAllContextUsage();
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	token.set('test-token');
	localStorage.clear();
});

afterEach(() => {
	activeThreadId.set(null);
	messages.set([]);
	clearAllContextUsage();
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	token.set(null);
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	localStorage.clear();
});

function setRunningCheckpoint(threadId: string): void {
	checkpointsByThread.set(new Map([[threadId, {
		checkpoints: [{
			id: 'checkpoint-1', trigger: 'model_switch', mode: 'warm', status: 'running',
			writerModel: 'model-source', error: null,
			startedAt: '2026-07-21T08:00:00Z',
		}],
		loading: false, error: null,
	}]]));
}

function checkpointHttpResponse() {
	return {
		ok: true,
		json: async () => ({
			checkpoints: [{
				id: 'checkpoint-1', trigger: 'model_switch', mode: 'warm',
				status: 'installed', writerModel: 'model-source', error: null,
				startedAt: '2026-07-21T08:00:00Z',
			}],
		}),
	};
}

describe('turn-terminal drawer activity truth', () => {
	it('applies a late context invalidation only to the viewed thread', () => {
		const staleUsage = {
			model_context_window: 200000,
			total_used: null,
			utilization: null,
			token_usage_available: false,
			stale_reason: 'provider_compaction',
			snapshot_id: 'ctx-late',
		};

		handleMessage({
			type: 'context_usage_updated',
			thread_id: 'bg',
			boundary: 'provider_compaction',
			context_usage: staleUsage,
		}, () => {});
		expect(getContextUsage('viewed')).toBeNull();

		handleMessage({
			type: 'context_usage_updated',
			thread_id: 'viewed',
			boundary: 'provider_compaction',
			context_usage: staleUsage,
		}, () => {});
		expect(getContextUsage('viewed')).toEqual({ ...staleUsage, thread_id: 'viewed' });
});

describe('checkpoint websocket invalidation', () => {

	it('treats checkpoint_changed as an invalidation-only hint', async () => {
		setRunningCheckpoint('viewed');
		const fetchMock = vi.fn(async () => checkpointHttpResponse());
		vi.stubGlobal('fetch', fetchMock);

		handleMessage({
			type: 'checkpoint_changed',
			thread_id: 'viewed',
			status: 'untrusted-frame-status',
		}, () => {});

		await vi.waitFor(() => {
			// The durable read decides the status; the frame's own status is
			// ignored. Because that read found an install, the chip's usage is
			// then re-read from the next-turn preview (#1562).
			const urls = fetchMock.mock.calls.map(call => String((call as unknown[])[0]));
			expect(urls).toHaveLength(2);
			expect(urls[0]).toContain('/api/threads/viewed/checkpoints');
			expect(urls[1]).toContain('/api/threads/viewed/context-snapshot/next-turn');
			expect(get(checkpointsByThread).get('viewed')?.checkpoints[0]?.status)
				.toBe('installed');
		});
	});

	it('settles stale Turn ownership when the backend durably queues the input', async () => {
		requestInput('viewed', 'client-input-queued');
		messages.set([{
			id: 'client-input-queued', thread_id: 'viewed', role: 'user',
			content: 'queue after stale admission',
			metadata: { input_id: 'client-input-queued' },
			created_at: '2026-07-21T08:00:01Z', _optimistic: true,
		}]);
		handleMessage({
			type: 'input_queued', thread_id: 'viewed', input_id: 'client-input-queued',
			cutover_operation_id: 'operation-1', queued_input_id: 'server-input-queued',
			queue_order: 1, created_at: '2026-07-21T08:00:02Z', duplicate: false,
		}, () => {});

		await vi.waitFor(() => {
			expect(getTurnState('viewed')).toMatchObject({
				phase: 'idle', pendingInputIds: [],
			});
			expect(get(messages)).toEqual([expect.objectContaining({
				id: 'client-input-queued',
				content: 'queue after stale admission',
				_optimistic: true,
			})]);
		});
	});

	it('reconciles the durable queued-input receipt without treating it as a checkpoint event', () => {
		requestInput('viewed', 'client-inbox-1');
		messages.set([{
			id: 'client-inbox-1', thread_id: 'viewed', role: 'user',
			content: 'keep this thought', metadata: { input_id: 'client-inbox-1' },
			created_at: '2026-07-22T11:59:59Z', _optimistic: true,
		}]);
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
		const received: any[] = [];
		const unsubscribe = on('input_queued', data => received.push(data));
		const receipt = {
			type: 'input_queued', queue_kind: 'messages',
			thread_id: 'viewed', input_id: 'client-inbox-1',
			message_id: 'canonical-inbox-1', queue_order: 1,
			created_at: '2026-07-22T12:00:00Z', delivery_state: 'queued',
			delivery_revision: 1,
		};

		handleMessage(receipt, () => {});
		unsubscribe();

		expect(received).toEqual([receipt]);
		expect(fetchMock).not.toHaveBeenCalled();
		expect(getTurnState('viewed')).toMatchObject({
			phase: 'idle', pendingInputIds: [],
		});
		expect(get(messages)).toEqual([expect.objectContaining({
			id: 'canonical-inbox-1',
			content: 'keep this thought',
			_optimistic: false,
			delivery_state: 'queued',
			delivery_revision: 1,
		})]);
	});

	it('keeps delivered revision 2 when a delayed queued revision 1 receipt arrives', () => {
		messages.set([{
			id: 'client-inbox-stale-1', thread_id: 'viewed', role: 'user',
			content: 'keep the delivered state',
			metadata: { input_id: 'client-inbox-stale-1' },
			created_at: '2026-07-22T12:00:00Z', _optimistic: true,
		}]);
		const queuedReceipt = {
			type: 'input_queued', queue_kind: 'messages',
			thread_id: 'viewed', input_id: 'client-inbox-stale-1',
			message_id: 'canonical-inbox-stale-1', queue_order: 1,
			created_at: '2026-07-22T12:00:01Z', delivery_state: 'queued',
			delivery_revision: 1,
		};

		handleMessage(queuedReceipt, () => {});
		handleMessage({
			type: 'message_added', thread_id: 'viewed',
			message: {
				id: 'canonical-inbox-stale-1', role: 'user',
				content: 'keep the delivered state',
				metadata: { input_id: 'client-inbox-stale-1' },
				created_at: '2026-07-22T12:00:02Z',
				delivery_state: 'delivered', delivery_revision: 2,
			},
		}, () => {});
		handleMessage(queuedReceipt, () => {});

		expect(get(messages)).toEqual([expect.objectContaining({
			id: 'canonical-inbox-stale-1',
			delivery_state: 'delivered',
			delivery_revision: 2,
		})]);
	});

	it('ingests singleton message_added seq and orders canonical delivery after current output', () => {
		messages.set([
			{
				id: 'current-output', thread_id: 'viewed', role: 'assistant', content: 'done',
				created_at: '2026-07-22T12:10:00Z', seq: 40,
			},
			{
				id: 'canonical-input', thread_id: 'viewed', role: 'user', content: 'steer',
				metadata: { input_id: 'client-input' }, created_at: '2026-07-22T12:00:00Z',
				delivery_state: 'queued', delivery_revision: 1,
			},
		]);

		handleMessage({
			type: 'message_added', thread_id: 'viewed', message: {
				id: 'canonical-input', role: 'user', content: 'steer',
				metadata: { input_id: 'client-input' }, created_at: '2026-07-22T12:00:00Z',
				delivery_state: 'delivered', delivery_error: null, delivery_revision: 2,
				seq: 41,
			},
		}, () => {});

		expect(get(messages).map(message => [message.id, message.seq])).toEqual([
			['current-output', 40], ['canonical-input', 41],
		]);
	});

	it('settles messages_added in one store update and preserves FIFO batch order', () => {
		messages.set([
			{
				id: 'current-output', thread_id: 'viewed', role: 'assistant', content: 'done',
				created_at: '2026-07-22T12:10:00Z', seq: 40,
			},
			{
				id: 'first-input', thread_id: 'viewed', role: 'user', content: 'first',
				metadata: { input_id: 'client-first' }, created_at: '2026-07-22T12:00:00Z',
				delivery_state: 'queued', delivery_revision: 1,
			},
			{
				id: 'second-input', thread_id: 'viewed', role: 'user', content: 'second',
				metadata: { input_id: 'client-second' }, created_at: '2026-07-22T12:01:00Z',
				delivery_state: 'queued', delivery_revision: 1,
			},
		]);
		let updates = 0;
		const unsubscribe = messages.subscribe(() => { updates += 1; });
		updates = 0;

		handleMessage({
			type: 'messages_added', thread_id: 'viewed', messages: [
			{
				id: 'first-input', role: 'user', content: 'first',
				metadata: { input_id: 'client-first' }, created_at: '2026-07-22T12:00:00Z',
				delivery_state: 'delivered', delivery_error: null, delivery_revision: 2, seq: 41,
			},
			{
				id: 'second-input', role: 'user', content: 'second',
				metadata: { input_id: 'client-second' }, created_at: '2026-07-22T12:01:00Z',
				delivery_state: 'delivered', delivery_error: null, delivery_revision: 2, seq: 42,
			},
		],
		}, () => {});
		unsubscribe();

		expect(updates).toBe(1);
		expect(get(messages).map(message => message.id)).toEqual([
			'current-output', 'first-input', 'second-input',
		]);
	});

	it('keeps a canonical queued inbox row queued when its post-admission error arrives', () => {
		requestInput('viewed', 'client-inbox-preflight-1');
		messages.set([{
			id: 'canonical-inbox-preflight-1', thread_id: 'viewed', role: 'user',
			content: 'keep my thought queued',
			metadata: { input_id: 'client-inbox-preflight-1' },
			created_at: '2026-07-22T12:00:00Z',
			delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		}]);

		handleWsError({
			thread_id: 'viewed', input_id: 'client-inbox-preflight-1',
			message_id: 'canonical-inbox-preflight-1',
			message: 'Provider preflight could not continue',
		});

		expect(get(messages).find(message => message.id === 'canonical-inbox-preflight-1'))
			.toEqual(expect.objectContaining({
			id: 'canonical-inbox-preflight-1',
			delivery_state: 'queued',
			delivery_error: null,
			delivery_revision: 1,
		}));
	});

	it('keeps an optimistic row queued when its authoritative error wins the receipt race', () => {
		requestInput('viewed', 'client-inbox-error-first-1');
		messages.set([{
			id: 'client-inbox-error-first-1', thread_id: 'viewed', role: 'user',
			content: 'do not invent a failed retry',
			metadata: { input_id: 'client-inbox-error-first-1' },
			created_at: '2026-07-22T12:00:00Z', _optimistic: true,
		}]);

		// The direct origin error can beat the fire-and-forget queue broadcast.
		// Its delivery fields are the server's authoritative durable truth.
		handleWsError({
			thread_id: 'viewed', input_id: 'client-inbox-error-first-1',
			message_id: 'canonical-inbox-error-first-1',
			message: 'Provider preflight could not continue',
			delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		});
		handleMessage({
			type: 'input_queued', queue_kind: 'messages',
			thread_id: 'viewed', input_id: 'client-inbox-error-first-1',
			message_id: 'canonical-inbox-error-first-1', queue_order: 1,
			created_at: '2026-07-22T12:00:01Z',
			delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		}, () => {});

		expect(get(messages).filter(message => message.role === 'user')).toEqual([
			expect.objectContaining({
				id: 'canonical-inbox-error-first-1', _optimistic: false,
				delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
			}),
		]);
	});
});

	it('turn_interrupted clears an off-thread active spinner', () => {
		applyLifecycle('bg', BG);
		expect(get(threadActivity)['bg']).toBe('active');

		handleTurnInterrupted(frame({ thread_id: 'bg' }, BG));

		expect(get(threadActivity)['bg']).toBeUndefined();
		expect(getTurnState('bg').phase).toBe('terminal');
	});

	it('off-thread error stops the spinner and leaves the update dot to the server', () => {
		applyLifecycle('bg', BG);

		handleWsError(frame({ thread_id: 'bg', message: 'boom' }, BG));

		expect(get(threadActivity)['bg']).toBeUndefined();
		expect(getTurnState('bg').phase).toBe('terminal');
	});

	it('error on the viewed thread clears its spinner without marking unread', () => {
		applyLifecycle('viewed', VIEWED);

		handleWsError(frame({ thread_id: 'viewed', message: 'boom' }, VIEWED));

		expect(get(threadActivity)['viewed']).toBeUndefined();
		expect(getTurnState('viewed').phase).toBe('terminal');
	});

	it('settles a source rejection when the input error arrives before the owner terminal', () => {
		requestInput('viewed', 'rejected-input');
		handleTurnActivity(frame({ thread_id: 'viewed' }, VIEWED));

		handleWsError({
			thread_id: 'viewed',
			input_id: 'rejected-input',
			message: 'provider rejected input',
		});

		expect(getTurnState('viewed')).toMatchObject({
			turnId: VIEWED.turnId,
			turnOrder: VIEWED.turnOrder,
			phase: 'active',
			pendingInputIds: [],
		});

		handleTurnComplete(frame({ thread_id: 'viewed' }, VIEWED));

		expect(getTurnState('viewed')).toMatchObject({
			turnId: VIEWED.turnId,
			turnOrder: VIEWED.turnOrder,
			phase: 'terminal',
			pendingInputIds: [],
		});
	});

	it('settles a source rejection when the owner terminal arrives before the input error', () => {
		requestInput('viewed', 'rejected-input');
		handleTurnActivity(frame({ thread_id: 'viewed' }, VIEWED));
		handleTurnComplete(frame({ thread_id: 'viewed' }, VIEWED));

		expect(getTurnState('viewed')).toMatchObject({
			turnId: VIEWED.turnId,
			turnOrder: VIEWED.turnOrder,
			phase: 'terminal',
			pendingInputIds: ['rejected-input'],
		});

		handleWsError({
			thread_id: 'viewed',
			input_id: 'rejected-input',
			message: 'provider rejected input',
		});

		expect(getTurnState('viewed')).toMatchObject({
			turnId: VIEWED.turnId,
			turnOrder: VIEWED.turnOrder,
			phase: 'terminal',
			pendingInputIds: [],
		});
	});

	it('viewed provider error remains a generic visible system error', () => {
		handleWsError({ thread_id: 'viewed', message: 'LLM call failed' });

		const row = get(messages).at(-1);
		expect(row?.role).toBe('system');
		expect(row?.content).toBe('Error: LLM call failed');
		expect(row?.metadata).toBeUndefined();
	});

	it('viewed harness error preserves harness attribution metadata', () => {
			handleWsError({
				thread_id: 'viewed',
				message: 'Codex turn failed: rate limited',
				error_type: 'harness_error',
				event_type: 'harness.error',
				harness: 'codex',
				cause: 'rate_limit',
				repair_owner: 'provider-engine',
			});

		const row = get(messages).at(-1);
		expect(row?.role).toBe('system');
		expect(row?.metadata?.type).toBe('harness_error');
			expect(row?.metadata?.event_type).toBe('harness.error');
			expect(row?.metadata?.harness).toBe('codex');
			expect(row?.metadata?.cause).toBe('rate_limit');
			expect(row?.metadata?.repair_owner).toBe('provider-engine');
		});

		it('turn_complete installs the compact receipt on its exact message', () => {
			applyLifecycle('viewed', VIEWED);
			messages.set([{
				id: 'a1', thread_id: 'viewed', role: 'assistant', content: 'ok',
				metadata: { model: 'native/test' },
				created_at: new Date().toISOString(),
			}]);
			const usage = {
				model_context_window: 1000, system_tokens: 1, tool_def_tokens: 0,
				history_tokens: 1, user_tokens: 0, total_used: 2,
				utilization: 0.002, was_trimmed: false, history_messages: 1,
			};
			handleTurnComplete(frame({
				thread_id: 'viewed', context_message_id: 'a1',
				context_usage: usage,
			}, VIEWED));

			expect(get(messages)[0].metadata).toEqual({
				model: 'native/test', context_usage: usage,
			});
			expect(getContextUsage('viewed')).toEqual({ ...usage, thread_id: 'viewed' });
		});

		it('keeps the previous reading when turn_complete could not measure', () => {
			applyLifecycle('viewed', VIEWED);
			const previous = {
				model_context_window: 1000, system_tokens: 1, tool_def_tokens: 0,
				history_tokens: 400, user_tokens: 0, total_used: 401,
				utilization: 0.401, was_trimmed: false, history_messages: 3,
				token_usage_available: true, token_source: 'measured' as const,
				thread_id: 'viewed',
			};
			setContextUsage('viewed', previous);

			handleTurnComplete(frame({
				thread_id: 'viewed',
				context_usage: { ...previous, total_used: null, token_usage_available: false },
			}, VIEWED));

			// Kept on screen, but marked stale so the next preview read replaces it.
			expect(getContextUsage('viewed')).toEqual({ ...previous, stale: true });
		});

		it('attaches a no-assistant turn receipt only to its exact system marker', () => {
			applyLifecycle('viewed', VIEWED);
			messages.set([
				{
					id: 'assistant-a', thread_id: 'viewed', role: 'assistant',
					content: 'Turn A', created_at: new Date().toISOString(),
					metadata: { model: 'native/test' },
				},
				{
					id: 'marker-b', thread_id: 'viewed', role: 'system',
					content: 'Turn B completed', created_at: new Date().toISOString(),
					metadata: { type: 'result' },
				},
			]);

			handleTurnComplete(frame({
				thread_id: 'viewed',
				context_message_id: 'marker-b',
				context_usage: { snapshot_id: 'ctx-b', total_used: 22 },
			}, VIEWED));

			const [assistantA, markerB] = get(messages);
			expect(assistantA.metadata).toEqual({ model: 'native/test' });
			expect(markerB.metadata?.context_usage).toEqual({
				snapshot_id: 'ctx-b', total_used: 22,
			});
		});

		it('does not fall back to the latest assistant when the exact id is absent', () => {
			applyLifecycle('viewed', VIEWED);
			messages.set([{
				id: 'assistant-a', thread_id: 'viewed', role: 'assistant',
				content: 'Turn A', created_at: new Date().toISOString(),
				metadata: { model: 'native/test' },
			}]);

			handleTurnComplete(frame({
				thread_id: 'viewed',
				context_usage: { snapshot_id: 'ctx-b', total_used: 22 },
			}, VIEWED));

			expect(get(messages)[0].metadata).toEqual({ model: 'native/test' });
		});

		it('does not fall back when the exact context message id is unknown', () => {
			applyLifecycle('viewed', VIEWED);
			messages.set([{
				id: 'assistant-a', thread_id: 'viewed', role: 'assistant',
				content: 'Turn A', created_at: new Date().toISOString(),
				metadata: { model: 'native/test' },
			}]);

			handleTurnComplete(frame({
				thread_id: 'viewed',
				context_message_id: 'missing-b',
				context_usage: { snapshot_id: 'ctx-b', total_used: 22 },
			}, VIEWED));

			expect(get(messages)[0].metadata).toEqual({ model: 'native/test' });
		});

	});
