import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import { subagentMeta } from '../stores/subagents';
import { setWs } from '../ws/state';
import { sendMessage, stopRequestedTurns, stopTurn } from '../ws/send';
import type { SubagentRef } from '../stores/subagents';
import {
	activeThreadId,
	messages,
	setThreadContent,
	streamContentByThread,
} from '../stores/threads';
import { handleMessage } from '../ws/handlers';
import {
	_resetTurnStateForTests,
	applyLifecycle,
	getTurnState,
} from '../ws/turnState';
import { token } from '../stores/auth';
import { recoverPendingInputs } from '../ws/pendingRecovery';
import { isAwaitingDelivery, isImminentDelivery } from '../messageDelivery';

const _SUBAGENT_REF: SubagentRef = {
	childThreadId: 'child-1',
	parentThreadId: 'parent-1',
	name: 'worker',
	provider: 'cc',
	status: 'running',
};

beforeEach(() => {
	subagentMeta.set(new Map());
	setWs(null);
	activeThreadId.set('main-1');
	messages.set([]);
	streamContentByThread.set(new Map());
	_resetTurnStateForTests();
	token.set('test-token');
});

function mockWs(): () => string[] {
	const sent: string[] = [];
	setWs({
		readyState: WebSocket.OPEN,
		send: (data: string) => { sent.push(data); },
	} as unknown as WebSocket);
	return () => sent;
}

describe('trusted delivery intent wire contract', () => {
	it('emits queue_after_turn only from the explicit queue action and ignores metadata spoofing', () => {
		const getLastSent = mockWs();

		sendMessage('main-1', 'queue deliberately', undefined, [], {
			delivery_intent: 'apply_now',
		}, 'queue_after_turn');

		const payload = JSON.parse(getLastSent().at(-1)!);
		expect(payload).toMatchObject({
			type: 'message',
			delivery_intent: 'queue_after_turn',
		});
		expect(get(messages).at(-1)?.metadata).toMatchObject({
			delivery_intent: 'queue_after_turn',
		});
	});

	it('keeps ordinary sends apply_now even when caller metadata tries to spoof Queue', () => {
		const getLastSent = mockWs();

		sendMessage('main-1', 'apply normally', undefined, [], {
			delivery_intent: 'queue_after_turn',
		});

		const payload = JSON.parse(getLastSent().at(-1)!);
		expect(payload).not.toHaveProperty('delivery_intent');
		expect(get(messages).at(-1)?.metadata).not.toHaveProperty('delivery_intent');
	});

	it('uses the same top-level Queue field for subagent feeds', () => {
		const getLastSent = mockWs();
		subagentMeta.set(new Map([['child-1', _SUBAGENT_REF]]));

		sendMessage('child-1', 'queue child', undefined, [], undefined, 'queue_after_turn');

		expect(JSON.parse(getLastSent().at(-1)!)).toMatchObject({
			type: 'subagent_feed',
			delivery_intent: 'queue_after_turn',
		});
	});
});

describe('authoritative input correlation and stop', () => {
	it('uses one fresh input_id for the payload, pending reducer entry, and optimistic row', () => {
		const getSent = mockWs();

		const inputId = sendMessage('main-1', 'hello');

		const payload = JSON.parse(getSent().at(-1)!);
		expect(inputId).toMatch(/^[0-9a-f-]{36}$/);
		expect(payload).toMatchObject({
			type: 'message', thread_id: 'main-1', content: 'hello', input_id: inputId,
		});
		expect(get(messages)).toEqual([
			expect.objectContaining({
				id: inputId,
				_optimistic: true,
				metadata: expect.objectContaining({ input_id: inputId }),
			}),
		]);
		expect(getTurnState('main-1').pendingInputIds).toEqual([inputId]);
	});

	it('treats repeated identical sends as distinct user intents', () => {
		const getSent = mockWs();

		const firstInputId = sendMessage('main-1', 'same thought');
		const secondInputId = sendMessage('main-1', 'same thought');

		expect(firstInputId).toMatch(/^[0-9a-f-]{36}$/);
		expect(secondInputId).toMatch(/^[0-9a-f-]{36}$/);
		expect(secondInputId).not.toBe(firstInputId);
		expect(getSent().map(payload => JSON.parse(payload))).toEqual([
			expect.objectContaining({
				type: 'message', content: 'same thought', input_id: firstInputId,
			}),
			expect.objectContaining({
				type: 'message', content: 'same thought', input_id: secondInputId,
			}),
		]);
		expect(get(messages)).toEqual([
			expect.objectContaining({ id: firstInputId, _optimistic: true }),
			expect.objectContaining({ id: secondInputId, _optimistic: true }),
		]);
		expect(getTurnState('main-1').pendingInputIds).toEqual([
			firstInputId,
			secondInputId,
		]);
	});

	it('reconciles an acknowledgement to canonical message identity and timestamp', () => {
		const getSent = mockWs();
		const inputId = sendMessage('main-1', 'hello')!;

		handleMessage({
			type: 'input_accepted',
			thread_id: 'main-1',
			input_id: inputId,
			message_id: 'db-message-1',
			created_at: '2026-07-14T12:34:56Z',
			turn_id: 'turn-A',
			turn_order: 4,
		}, () => {});

		expect(getSent()).toHaveLength(1);
		expect(get(messages)).toEqual([
				expect.objectContaining({
					id: 'db-message-1',
					created_at: '2026-07-14T12:34:56Z',
					_optimistic: false,
					delivery_state: 'delivered',
					delivery_error: null,
					metadata: expect.objectContaining({ input_id: inputId }),
				}),
		]);
		expect(getTurnState('main-1')).toMatchObject({
			turnId: 'turn-A', turnOrder: 4, phase: 'active', pendingInputIds: [],
		});
	});

	it('does not delete a canonical accepted row on a late duplicate rejection', () => {
		mockWs();
		const inputId = sendMessage('main-1', 'accepted')!;
		handleMessage({
			type: 'input_accepted', thread_id: 'main-1', input_id: inputId,
			message_id: 'canonical-accepted', created_at: '2026-07-14T12:34:56Z',
			turn_id: 'turn-A', turn_order: 4,
		}, () => {});

		handleMessage({
			type: 'error', thread_id: 'main-1', input_id: inputId,
			message: 'late duplicate rejection',
		}, () => {});

		expect(get(messages).some(message => message.id === 'canonical-accepted')).toBe(true);
		expect(getTurnState('main-1')).toMatchObject({
			turnId: 'turn-A', turnOrder: 4, phase: 'active', pendingInputIds: [],
		});
	});

	it('retains one rejected pending input as failed without clearing its predecessor', () => {
		mockWs();
		applyLifecycle('main-1', { turnId: 'turn-A', turnOrder: 4 });
		setThreadContent('main-1', 'predecessor stream');
		const rejectedId = sendMessage('main-1', 'replace me')!;
		const survivorId = sendMessage('main-1', 'keep pending')!;

		handleMessage({
			type: 'error', thread_id: 'main-1', input_id: rejectedId,
			message: 'preflight rejected',
		}, () => {});

		expect(get(messages).find(message => message.id === rejectedId)).toMatchObject({
			content: 'replace me',
			delivery_state: 'failed',
			delivery_error: 'preflight rejected',
		});
		expect(get(messages).some(message => message.id === survivorId)).toBe(true);
		expect(get(streamContentByThread).get('main-1')).toBe('predecessor stream');
		expect(getTurnState('main-1')).toMatchObject({
			turnId: 'turn-A', turnOrder: 4, phase: 'active',
			pendingInputIds: [survivorId],
		});
	});

	it('upserts one canonical row across queued and delivered message updates', () => {
		mockWs();
		const inputId = sendMessage('main-1', 'hold this thought')!;

		handleMessage({
			type: 'message_added', thread_id: 'main-1',
			message: {
				id: 'canonical-input-1', role: 'user', content: 'hold this thought',
				metadata: { input_id: inputId },
				created_at: '2026-07-22T12:00:00Z',
				delivery_state: 'queued', delivery_error: null,
			},
		}, () => {});

		expect(get(messages)).toEqual([expect.objectContaining({
			id: 'canonical-input-1', _optimistic: false,
			delivery_state: 'queued', delivery_error: null,
		})]);

		handleMessage({
			type: 'message_added', thread_id: 'main-1',
			message: {
				id: 'canonical-input-1', role: 'user', content: 'hold this thought',
				metadata: { input_id: inputId },
				created_at: '2026-07-22T12:00:00Z',
				delivery_state: 'delivered', delivery_error: null,
			},
		}, () => {});

		expect(get(messages)).toEqual([expect.objectContaining({
			id: 'canonical-input-1', _optimistic: false,
			delivery_state: 'delivered', delivery_error: null,
		})]);
	});

	it('does not accept a noncanonical delivery state on a message row', () => {
		handleMessage({
			type: 'message_added', thread_id: 'main-1',
			message: {
				id: 'future-state-input', role: 'user', content: 'future state',
				created_at: '2026-07-22T12:00:00Z',
				delivery_state: 'settled_elsewhere', delivery_revision: 1,
			},
		}, () => {});

		expect(get(messages)).toEqual([expect.not.objectContaining({
			delivery_state: 'settled_elsewhere',
		})]);
	});

	it('does not make a noncanonical error-receipt state authoritative', () => {
		mockWs();
		const inputId = sendMessage('main-1', 'future ending')!;

		handleMessage({
			type: 'error', thread_id: 'main-1', input_id: inputId,
			delivery_state: 'settled_elsewhere', delivery_error: 'future ending',
		}, () => {});

		expect(get(messages).find(message => message.id === inputId)).toMatchObject({
			delivery_state: 'failed', delivery_error: 'future ending',
		});
	});

	it('does not let a stale delivery revision regress a canonical message', () => {
		mockWs();
		const inputId = sendMessage('main-1', 'keep the newer delivery state')!;
		const base = {
			id: 'canonical-input-revision', role: 'user',
			content: 'keep the newer delivery state',
			metadata: { input_id: inputId },
			created_at: '2026-07-22T12:00:00Z',
		};

		handleMessage({
			type: 'message_added', thread_id: 'main-1',
			message: {
				...base, delivery_state: 'delivered', delivery_error: null,
				delivery_revision: 2,
			},
		}, () => {});
		handleMessage({
			type: 'message_added', thread_id: 'main-1',
			message: {
				...base, delivery_state: 'queued', delivery_error: null,
				delivery_revision: 1,
			},
		}, () => {});

		expect(get(messages)).toEqual([expect.objectContaining({
			id: 'canonical-input-revision',
			delivery_state: 'delivered',
			delivery_revision: 2,
		})]);
	});

	it('sends Stop without a local identity, preserves a known one, and does not optimistically terminalize', () => {
		const getSent = mockWs();
		stopTurn('main-1');
		expect(JSON.parse(getSent()[0]!)).toEqual({
			type: 'stop', thread_id: 'main-1',
		});

		applyLifecycle('main-1', { turnId: 'turn-A', turnOrder: 4 });
		stopTurn('main-1');
		expect(JSON.parse(getSent().at(-1)!)).toEqual({
			type: 'stop', thread_id: 'main-1', turn_id: 'turn-A',
		});
		expect(getTurnState('main-1').phase).toBe('active');
	});

	it('records the exact turn Stop was asked of so the control can acknowledge it', () => {
		// Stop used to leave no trace, so a Stop that worked and a Stop that did
		// nothing looked identical to the user (USER-REPORTS #6b).
		mockWs();
		applyLifecycle('main-1', { turnId: 'turn-A', turnOrder: 4 });
		stopTurn('main-1');

		expect(get(stopRequestedTurns).get('main-1')).toBe('turn-A');

		// The acknowledgement is keyed by turn id, so the next turn starts clean.
		applyLifecycle('main-1', { turnId: 'turn-B', turnOrder: 5 });
		expect(get(stopRequestedTurns).get('main-1')).not.toBe('turn-B');
	});

	it('sends an exact acknowledged Stop through the delegated task route', () => {
		const getSent = mockWs();
		subagentMeta.set(new Map([['child-1', {
			..._SUBAGENT_REF, status: 'working' as const,
		}]]));
		applyLifecycle('child-1', { turnId: 'turn-child', turnOrder: 7 });

		stopTurn('child-1');

		expect(JSON.parse(getSent().at(-1)!)).toEqual({
			type: 'subagent_stop', thread_id: 'child-1', turn_id: 'turn-child',
		});
	});

	it('includes correlation on subagent input without optimistic status mutation', () => {
		const getSent = mockWs();
		const idle = { ..._SUBAGENT_REF, status: 'idle' as const };
		subagentMeta.set(new Map([['child-1', idle]]));

		const inputId = sendMessage('child-1', 'work')!;
		const payload = JSON.parse(getSent().at(-1)!);
		expect(payload).toMatchObject({
			type: 'subagent_feed', thread_id: 'child-1', input_id: inputId,
		});
		expect(get(subagentMeta).get('child-1')?.status).toBe('idle');
	});

	it('recovers a lost acknowledgement from existing paged message history', async () => {
		mockWs();
		const inputId = sendMessage('main-1', 'durable')!;
		let fetchedUrl: RequestInfo | URL | undefined;
		const fetchPage = vi.fn(async (input: RequestInfo | URL) => {
			fetchedUrl = input;
			return new Response(JSON.stringify({
				messages: [{
					id: 'canonical-1',
					role: 'user',
					content: 'durable',
					created_at: '2026-07-14T13:00:00Z',
					metadata: { input_id: inputId },
				}],
				has_more: false,
				oldest_id: 'canonical-1',
			}), { status: 200 });
		});

		await recoverPendingInputs(fetchPage as typeof fetch);

		expect(fetchPage).toHaveBeenCalledOnce();
		expect(String(fetchedUrl)).toContain('/api/threads/main-1/messages?');
		expect(getTurnState('main-1')).toEqual({
			turnId: null, turnOrder: null, phase: 'idle', pendingInputIds: [],
			visibleRootThreadId: null, startedAt: null,
		});
		expect(get(messages)[0]).toMatchObject({
			id: 'canonical-1', created_at: '2026-07-14T13:00:00Z', _optimistic: false,
		});
	});

	it('does not query history when no thread retains pending input', async () => {
		const fetchPage = vi.fn();
		await recoverPendingInputs(fetchPage as typeof fetch);
		expect(fetchPage).not.toHaveBeenCalled();
	});
});

describe('durable checkpoint admission', () => {
	it('sends a message during a checkpoint as ordinary queued input', () => {
		// The reported bug, from the client side: a message sent while a
		// checkpoint held the thread used to be tracked in a side store and
		// rendered only as a chip above the composer. It is now an ordinary
		// pending input in the transcript, so a reload cannot erase it.
		const getSent = mockWs();
		const inputId = sendMessage('main-1', 'wait for checkpoint')!;

		expect(JSON.parse(getSent().at(-1)!)).toMatchObject({
			type: 'message',
			thread_id: 'main-1',
			content: 'wait for checkpoint',
			input_id: inputId,
		});
		expect(getTurnState('main-1').pendingInputIds).toEqual([inputId]);
	});

});

describe('the optimistic echo during a running turn', () => {
	it('claims nothing the server owns, yet holds the waiting region below the reply', () => {
		// #1087: a mid-turn send has not earned its durable position. The
		// client writes no server field to say so — the echo plus the live
		// turn is the whole claim — so the row renders below the reply being
		// written and stays there when its receipt replaces the claim with
		// server truth.
		mockWs();
		applyLifecycle('main-1', { turnId: 'turn-A', turnOrder: 4 });

		const inputId = sendMessage('main-1', 'steer me')!;

		const echo = get(messages).find(m => m.id === inputId)!;
		expect(echo._optimistic).toBe(true);
		expect(echo.delivery_state).toBeUndefined();
		expect(isAwaitingDelivery(echo, true)).toBe(true);
		expect(isImminentDelivery(echo, true)).toBe(true);
	});

	it('puts no client-only delivery state on the wire', () => {
		const getSent = mockWs();
		applyLifecycle('main-1', { turnId: 'turn-A', turnOrder: 4 });

		sendMessage('main-1', 'steer me');

		const messageFrames = getSent().map(frame => JSON.parse(frame))
			.filter(payload => payload.type === 'message');
		expect(messageFrames).toHaveLength(1);
		expect(messageFrames[0].delivery_state).toBeUndefined();
		expect(messageFrames[0].content).toBe('steer me');
	});

	it('keeps an idle-thread echo an ordinary row at the live edge', () => {
		// No turn is running, so the send starts the next one: the row lands
		// at the live edge, and nothing about that wait needs a region.
		mockWs();

		const inputId = sendMessage('main-1', 'starts the turn')!;

		const echo = get(messages).find(m => m.id === inputId)!;
		expect(isAwaitingDelivery(echo, false)).toBe(false);
		expect(isImminentDelivery(echo, false)).toBe(false);
	});
});
