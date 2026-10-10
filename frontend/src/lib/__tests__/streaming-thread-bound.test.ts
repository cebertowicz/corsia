import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

import {
	activeThreadId,
	activeToolsByThread,
	messages,
	streamContentByThread,
	liveReasoningByThread,
	lastAppliedSeqByThread,
	pendingDeltasByThread,
	setThreadContent,
	type Message,
} from '../stores/threads';
import { handleMessage } from '../ws/handlers';
import { on } from '../ws/events';
import { beginPendingSnapshot } from '../ws/streamState';
import {
	startTypewriter,
	cancelThreadTypewriters,
	_resetTypewritersForTests,
	_getActiveTypewriterCount,
} from '../ws/typewriter';
import {
	_resetTurnStateForTests,
	applyLifecycle,
	getTurnState,
	type TurnIdentity,
} from '../ws/turnState';

const TURN_A: TurnIdentity = { turnId: 'turn-A', turnOrder: 1 };
const TURN_B: TurnIdentity = { turnId: 'turn-B', turnOrder: 2 };

function activate(threadId: string, identity: TurnIdentity): void {
	applyLifecycle(threadId, identity);
}

function lifecycleFrame(
	payload: Record<string, unknown>,
	identity: TurnIdentity,
): Record<string, unknown> {
	return {
		...payload,
		turn_id: identity.turnId,
		turn_order: identity.turnOrder,
	};
}

function resetStores() {
	activeThreadId.set(null);
	messages.set([]);
	streamContentByThread.set(new Map());
	liveReasoningByThread.set(new Map());
	activeToolsByThread.set(new Map());
	lastAppliedSeqByThread.set(new Map());
	pendingDeltasByThread.set(new Map());
	_resetTypewritersForTests();
	_resetTurnStateForTests();
}

function assistantMsg(id: string, sid: string, content: string, typewriterPos?: number): Message {
	return {
		id,
		thread_id: sid,
		role: 'assistant',
		content,
		created_at: '2026-05-22T00:00:00Z',
		typewriterPos,
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	resetStores();
});

afterEach(() => {
	resetStores();
	vi.useRealTimers();
});

describe('#117 — typewriter is bound to session-id and cleans up on switch', () => {
	it('startTypewriter advances the message in its session while active', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		const longContent = 'a'.repeat(200);
		messages.set([assistantMsg('msg-1', 'A', longContent, 0)]);

		startTypewriter('A', 'msg-1', 0, 200, TURN_A);
		expect(_getActiveTypewriterCount('A')).toBe(1);

		vi.advanceTimersByTime(33);

		const after = get(messages).find(m => m.id === 'msg-1');
		expect(after?.typewriterPos).toBeGreaterThan(0);
		expect(after?.typewriterPos).toBeLessThan(200);
	});

	it('typewriter for session A does NOT mutate session B view after a switch', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([assistantMsg('msg-A', 'A', 'streaming content for session A', 0)]);
		startTypewriter('A', 'msg-A', 0, 32, TURN_A);
		activeThreadId.set('B');
		messages.set([assistantMsg('msg-B', 'B', 'session B message', undefined)]);
		const msgBBefore = get(messages).find(m => m.id === 'msg-B');
		expect(msgBBefore?.typewriterPos).toBeUndefined();
		vi.advanceTimersByTime(33 * 20);
		const msgBAfter = get(messages).find(m => m.id === 'msg-B');
		expect(msgBAfter?.typewriterPos).toBeUndefined();
		expect(_getActiveTypewriterCount('A')).toBe(0);
	});

	it('no `typewriter_tick` events fire for a session the user has left', () => {
		const ticks: Array<{ messageId: string; threadId?: string }> = [];
		const off = on('typewriter_tick', (d) => ticks.push(d));
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([assistantMsg('msg-A', 'A', 'a'.repeat(50), 0)]);
		startTypewriter('A', 'msg-A', 0, 50, TURN_A);

		vi.advanceTimersByTime(33 * 2);
		const ticksForA = ticks.length;
		expect(ticksForA).toBeGreaterThan(0);
		activeThreadId.set('B');
		messages.set([assistantMsg('msg-B', 'B', 'unrelated', undefined)]);
		const ticksBefore = ticks.length;
		vi.advanceTimersByTime(33 * 30);
		expect(ticks.length - ticksBefore).toBeLessThanOrEqual(1);
		off();
	});

	it('cancelThreadTypewriters() clears running timers + reveals partial content', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([assistantMsg('msg-A', 'A', 'hello world', 4)]);
		startTypewriter('A', 'msg-A', 4, 11, TURN_A);
		expect(_getActiveTypewriterCount('A')).toBe(1);
		cancelThreadTypewriters('A');
		expect(_getActiveTypewriterCount('A')).toBe(0);
		const after = get(messages).find(m => m.id === 'msg-A');
		expect(after?.typewriterPos).toBeUndefined();
	});

	it('cancelThreadTypewriters() is scoped — does not touch other threads', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		activate('B', TURN_B);
		messages.set([
			assistantMsg('msg-A', 'A', 'session A msg', 3),
			assistantMsg('msg-B', 'B', 'session B msg', 5),
		]);
		startTypewriter('A', 'msg-A', 3, 13, TURN_A);
		startTypewriter('B', 'msg-B', 5, 12, TURN_B);
		expect(_getActiveTypewriterCount()).toBe(2);
		cancelThreadTypewriters('A');
		expect(_getActiveTypewriterCount('A')).toBe(0);
		expect(_getActiveTypewriterCount('B')).toBe(1);
		const msgs = get(messages);
		expect(msgs.find(m => m.id === 'msg-A')?.typewriterPos).toBeUndefined();
		expect(msgs.find(m => m.id === 'msg-B')?.typewriterPos).toBe(5);
	});

	it('starting a typewriter twice for the same (session, message) is idempotent', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([assistantMsg('msg-A', 'A', 'content', 0)]);
		startTypewriter('A', 'msg-A', 0, 7, TURN_A);
		startTypewriter('A', 'msg-A', 0, 7, TURN_A);
		expect(_getActiveTypewriterCount('A')).toBe(1);
	});

	it('turn_complete handler cancels typewriters for the completing session', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([assistantMsg('msg-A', 'A', 'streamed reply', 3)]);
		startTypewriter('A', 'msg-A', 3, 14, TURN_A);
		handleMessage(lifecycleFrame({ type: 'turn_complete', thread_id: 'A' }, TURN_A), () => {});
		expect(_getActiveTypewriterCount('A')).toBe(0);
		expect(get(messages).find(m => m.id === 'msg-A')?.typewriterPos).toBeUndefined();
	});

	it('turn_cancelled handler cancels typewriters for the cancelled session', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([assistantMsg('msg-A', 'A', 'partial reply', 2)]);
		startTypewriter('A', 'msg-A', 2, 13, TURN_A);
		handleMessage(lifecycleFrame({ type: 'turn_cancelled', thread_id: 'A' }, TURN_A), () => {});
		expect(_getActiveTypewriterCount('A')).toBe(0);
	});

	it('step_text starts a session-bound typewriter only for the active session', () => {
		activeThreadId.set('A');
		handleMessage(lifecycleFrame({
			type: 'step_text',
			thread_id: 'B',
			message_id: 'msg-B',
			content: 'B reply',
			metadata: {},
			created_at: '2026-07-14T12:00:00Z',
		}, TURN_B), () => {});
		expect(_getActiveTypewriterCount('B')).toBe(0);
		handleMessage(lifecycleFrame({
			type: 'step_text',
			thread_id: 'A',
			message_id: 'msg-A',
			content: 'A reply',
			metadata: {},
			created_at: '2026-07-14T12:00:01Z',
		}, TURN_A), () => {});
		expect(_getActiveTypewriterCount('A')).toBe(1);
	});

	it('typewriter that previously had streamed deltas does NOT animate (skip replay)', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		setThreadContent('A', 'live-streamed-content');
		handleMessage(lifecycleFrame({
			type: 'step_text',
			thread_id: 'A',
			message_id: 'msg-A',
			content: 'live-streamed-content',
			metadata: {},
			created_at: '2026-07-14T12:00:00Z',
		}, TURN_A), () => {});
		expect(_getActiveTypewriterCount('A')).toBe(0);
		const m = get(messages).find(x => x.id === 'msg-A');
		expect(m?.typewriterPos).toBeUndefined();
	});
});

describe('Assistant Step live finalization', () => {
	it('uses stream order for replay while preserving transcript order', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);

		handleMessage(lifecycleFrame({
			type: 'step_text', thread_id: 'A', message_id: 'step-ordered',
			content: 'first step', metadata: {},
			created_at: '2026-07-14T12:00:01Z', seq: 42, stream_seq: 2,
			finalized_tool_ids: [],
		}, TURN_A), () => {});
		handleMessage(lifecycleFrame({
			type: 'text_delta', thread_id: 'A', content: 'next generation', seq: 3,
		}, TURN_A), () => {});

		expect(get(messages).find(message => message.id === 'step-ordered')?.seq).toBe(42);
		expect(get(lastAppliedSeqByThread).get('A')).toEqual({ ...TURN_A, seq: 3 });
		expect(get(streamContentByThread).get('A')).toBe('next generation');
	});

	it('upserts by durable id, trusts server time, and consumes only finalized tools', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([{
			id: 'step-1', thread_id: 'A', role: 'assistant', content: 'checking',
			created_at: '2026-07-14T12:00:01Z', metadata: { tool_calls: [] },
		}]);
		activeToolsByThread.set(new Map([['A', [
			{ id: 'tool-a', name: 'Read', hint: 'a', status: 'done' },
			{ id: 'tool-b', name: 'Read', hint: 'b', status: 'running' },
		]]]));

		const finalized = lifecycleFrame({
			type: 'step_text', thread_id: 'A', message_id: 'step-1',
			content: 'checking', created_at: '2026-07-14T11:59:59Z',
			metadata: { tool_calls: [{ id: 'tool-a', result: 'done' }] },
			finalized_tool_ids: ['tool-a'],
		}, TURN_A);
		handleMessage(finalized, () => {});
		handleMessage(finalized, () => {});

		const rows = get(messages);
		expect(rows).toHaveLength(1);
		expect(rows[0]).toMatchObject({
			id: 'step-1', created_at: '2026-07-14T11:59:59Z',
			metadata: { tool_calls: [{ id: 'tool-a', result: 'done' }] },
		});
		expect(get(activeToolsByThread).get('A')).toEqual([
			expect.objectContaining({ id: 'tool-b', status: 'running' }),
		]);
	});

	it('inserts a late finalized predecessor before optimistic successor input', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		messages.set([
			{
				id: 'prior-user', thread_id: 'A', role: 'user', content: 'first',
				created_at: '2026-07-14T12:00:00Z',
			},
			{
				id: 'pending-successor', thread_id: 'A', role: 'user', content: 'redirect',
				created_at: '2026-07-14T12:00:03Z', _optimistic: true,
			},
		]);

		handleMessage(lifecycleFrame({
			type: 'step_text', thread_id: 'A', message_id: 'predecessor-step',
			content: 'finished tool work', metadata: {},
			created_at: '2026-07-14T12:00:02Z', finalized_tool_ids: [],
		}, TURN_A), () => {});

		expect(get(messages).map(message => message.id)).toEqual([
			'prior-user', 'predecessor-step', 'pending-successor',
		]);
	});

	it('orders and dedupes a delayed peer message_added before its live assistant step', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);

		handleMessage(lifecycleFrame({
			type: 'step_text', thread_id: 'A', message_id: 'successor-step',
			content: 'fast response', metadata: {},
			created_at: '2026-07-14T12:00:03Z', finalized_tool_ids: [],
		}, TURN_A), () => {});
		const delayedInput = {
			type: 'message_added', thread_id: 'A',
			message: {
				id: 'successor-input', role: 'user', content: 'next', metadata: {},
				created_at: '2026-07-14T12:00:02Z',
			},
		};
		handleMessage(delayedInput, () => {});
		handleMessage(delayedInput, () => {});

		expect(get(messages).map(message => message.id)).toEqual([
			'successor-input', 'successor-step',
		]);
	});
});

describe('#118 — turn_complete does NOT refetch / replace live messages', () => {
	it('turn_complete does NOT clear or replace the messages store', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		const live = assistantMsg('msg-A', 'A', 'fully streamed content', undefined);
		messages.set([live]);
		handleMessage(lifecycleFrame({
			type: 'turn_complete',
			thread_id: 'A',
			step_ids: ['msg-A'],
			context_usage: null,
		}, TURN_A), () => {});
		const after = get(messages);
		expect(after.length).toBe(1);
		expect(after[0].id).toBe('msg-A');
		expect(after[0].content).toBe('fully streamed content');
	});

	it('turn_complete preserves the live message across multiple steps', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		const msg1 = assistantMsg('step-1', 'A', 'first reasoning step', undefined);
		const msg2 = assistantMsg('step-2', 'A', 'second response', undefined);
		const msg3 = assistantMsg('step-3', 'A', 'final answer', undefined);
		messages.set([msg1, msg2, msg3]);
		handleMessage(lifecycleFrame({
			type: 'turn_complete',
			thread_id: 'A',
			step_ids: ['step-1', 'step-2', 'step-3'],
		}, TURN_A), () => {});
		const after = get(messages);
		expect(after.map(m => m.id)).toEqual(['step-1', 'step-2', 'step-3']);
	});

	it('turn_complete still clears session buffers (live deltas no longer in flight)', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		setThreadContent('A', 'in-flight content that step_text already drained');
		handleMessage(lifecycleFrame({ type: 'turn_complete', thread_id: 'A' }, TURN_A), () => {});
		expect(get(streamContentByThread).get('A')).toBeUndefined();
	});

	it('turn_complete for a non-active session does not touch messages store at all', () => {
		activeThreadId.set('A');
		activate('B', TURN_B);
		const aMsg = assistantMsg('msg-A', 'A', 'A content', undefined);
		messages.set([aMsg]);
		handleMessage(lifecycleFrame({ type: 'turn_complete', thread_id: 'B' }, TURN_B), () => {});
		const after = get(messages);
		expect(after.length).toBe(1);
		expect(after[0].id).toBe('msg-A');
	});
});

describe('#117 + #118 — combined regression: rapid switch + turn_complete', () => {
	it('rapid switch A → B → A during streaming + turn_complete leaves both threads clean', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		activate('B', TURN_B);
		const msgA = assistantMsg('msg-A', 'A', 'streaming A content', 0);
		messages.set([msgA]);
		startTypewriter('A', 'msg-A', 0, 20, TURN_A);
		activeThreadId.set('B');
		messages.set([assistantMsg('msg-B', 'B', 'session B history', undefined)]);
		cancelThreadTypewriters('A');
		const msgB1 = assistantMsg('msg-B1', 'B', 'replying in B', 0);
		messages.update(m => [...m, msgB1]);
		startTypewriter('B', 'msg-B1', 0, 13, TURN_B);
		activeThreadId.set('A');
		messages.set([msgA]);
		cancelThreadTypewriters('B');
		handleMessage(lifecycleFrame({ type: 'turn_complete', thread_id: 'A' }, TURN_A), () => {});
		expect(_getActiveTypewriterCount()).toBe(0);
		const after = get(messages);
		expect(after.length).toBe(1);
		expect(after[0].id).toBe('msg-A');
		expect(after[0].content).toBe('streaming A content');
	});
});

describe('generation ordering isolation', () => {
	it('keeps successor deltas queued until an exact successor snapshot merges them', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		beginPendingSnapshot('A');

		handleMessage(lifecycleFrame({
			type: 'text_delta', thread_id: 'A', content: 'B-after-snapshot', seq: 2,
		}, TURN_B), () => {});

		expect(get(streamContentByThread).get('A')).toBeUndefined();
		expect(get(pendingDeltasByThread).get('A')).toMatchObject({
			identity: TURN_B,
		});

		handleMessage(lifecycleFrame({
			type: 'session_snapshot', thread_id: 'A',
			state: { content: 'B-snapshot+', reasoning: '', active_tools: [], seq: 1 },
		}, TURN_B), () => {});

		expect(get(streamContentByThread).get('A')).toBe('B-snapshot+B-after-snapshot');
		expect(get(pendingDeltasByThread).has('A')).toBe(false);
	});

	it('releases queued successor deltas when the sole snapshot is stale', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		beginPendingSnapshot('A');

		handleMessage(lifecycleFrame({
			type: 'text_delta', thread_id: 'A', content: 'B-live', seq: 1,
		}, TURN_B), () => {});

		expect(get(streamContentByThread).get('A')).toBeUndefined();
		expect(get(pendingDeltasByThread).get('A')).toMatchObject({
			identity: TURN_B,
		});

		handleMessage(lifecycleFrame({
			type: 'session_snapshot', thread_id: 'A',
			state: { content: 'stale-A', reasoning: '', active_tools: [], seq: 0 },
		}, TURN_A), () => {});

		expect(get(streamContentByThread).get('A')).toBe('B-live');
		expect(get(pendingDeltasByThread).has('A')).toBe(false);
	});

	it('stale A terminal cannot cancel B typewriters or clear B buffers', () => {
		activeThreadId.set('A');
		activate('A', TURN_A);
		activate('A', TURN_B);
		messages.set([assistantMsg('msg-B', 'A', 'successor content', 0)]);
		setThreadContent('A', 'B buffer');
		startTypewriter('A', 'msg-B', 0, 17, TURN_B);

		handleMessage(lifecycleFrame({
			type: 'turn_complete', thread_id: 'A',
		}, TURN_A), () => {});

		expect(_getActiveTypewriterCount('A')).toBe(1);
		expect(get(streamContentByThread).get('A')).toBe('B buffer');
		vi.advanceTimersByTime(33);
		expect(get(messages)[0].typewriterPos).toBeGreaterThan(0);
	});

	it('stale A stream, tool, and snapshot frames cannot mutate B generation state', () => {
		activeThreadId.set('A');
		activate('A', TURN_B);
		handleMessage(lifecycleFrame({
			type: 'text_delta', thread_id: 'A', content: 'B1', seq: 1,
		}, TURN_B), () => {});
		handleMessage(lifecycleFrame({
			type: 'text_delta', thread_id: 'A', content: 'STALE', seq: 999,
		}, TURN_A), () => {});
		handleMessage(lifecycleFrame({
			type: 'text_delta', thread_id: 'A', content: 'B2', seq: 2,
		}, TURN_B), () => {});
		handleMessage(lifecycleFrame({
			type: 'tool_start', thread_id: 'A', tool_id: 'tool-B', name: 'read',
		}, TURN_B), () => {});
		handleMessage(lifecycleFrame({
			type: 'tool_done', thread_id: 'A', tool_id: 'tool-B', result: 'stale',
		}, TURN_A), () => {});
		handleMessage(lifecycleFrame({
			type: 'session_snapshot', thread_id: 'A',
			state: { content: 'stale snapshot', reasoning: '', active_tools: [], seq: 999 },
		}, TURN_A), () => {});

		expect(get(streamContentByThread).get('A')).toBe('B1B2');
		expect(get(activeToolsByThread).get('A')).toEqual([
			expect.objectContaining({ id: 'tool-B', status: 'running' }),
		]);
		expect(get(lastAppliedSeqByThread).get('A')).toEqual({
			...TURN_B, seq: 2,
		});
	});

	it('a snapshot with no live turn stops the spinner it left behind', () => {
		// USER-REPORTS #6: the turn was durably `interrupted`, no active turn
		// existed anywhere, and the UI still showed a running tool block and a
		// Stop button. The snapshot said so and the client ignored it.
		activeThreadId.set('A');
		activate('A', TURN_B);
		handleMessage(lifecycleFrame({
			type: 'tool_start', thread_id: 'A', tool_id: 'tool-B', name: 'read',
		}, TURN_B), () => {});
		handleMessage(lifecycleFrame({
			type: 'text_delta', thread_id: 'A', content: 'half a sentence', seq: 1,
		}, TURN_B), () => {});

		handleMessage({
			type: 'session_snapshot', thread_id: 'A',
			turn_id: null, turn_order: null, state: null,
		}, () => {});

		expect(getTurnState('A').phase).toBe('terminal');
		expect(get(activeToolsByThread).get('A')).toBeUndefined();
		expect(get(streamContentByThread).get('A')).toBeUndefined();
	});
});
