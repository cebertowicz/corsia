import { beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from '../stores/auth';
import { subagentMeta } from '../stores/subagents';
import { activeThreadId, messages } from '../stores/threads';
import { handleMessage } from './handlers';
import { recoverPendingInputs } from './pendingRecovery';
import { _unconfirmedFrameIdsForTests, sendMessage } from './send';
import { setWs } from './state';
import { _resetTurnStateForTests, getTurnState, requestInput } from './turnState';

const THREAD = 'main-1';
const STORED_AT = '2026-11-05T18:00:00Z';

let frames: string[];

/** The live socket, recording every raw frame so a resend can be compared byte for byte. */
function mockSocket(): void {
	frames = [];
	setWs({
		readyState: WebSocket.OPEN,
		send: (data: string) => { frames.push(data); },
	} as unknown as WebSocket);
}

function historyResponse(rows: unknown[], hasMore = false, oldestId: string | null = null) {
	return new Response(JSON.stringify({
		messages: rows, has_more: hasMore, oldest_id: oldestId,
	}), { status: 200 });
}

const emptyHistory = async () => historyResponse([]);

beforeEach(() => {
	mockSocket();
	subagentMeta.set(new Map());
	activeThreadId.set(THREAD);
	messages.set([]);
	_resetTurnStateForTests();
	token.set('test-token');
});

describe('resending an input the server never received', () => {
	it('puts the swallowed frame back on the socket once, byte for byte', async () => {
		const inputId = sendMessage(THREAD, 'never arrived')!;
		const frame = frames[0];

		await recoverPendingInputs(emptyHistory);

		expect(frames).toHaveLength(2);
		expect(frames[1]).toBe(frame);
		expect(JSON.parse(frames[1]).input_id).toBe(inputId);
		// The resend is not a claim of delivery: the row stays pending until the
		// server answers this socket, and a second recovery can ask again.
		expect(getTurnState(THREAD).pendingInputIds).toEqual([inputId]);
	});

	it('resends a subagent feed the same way', async () => {
		subagentMeta.set(new Map([[THREAD, {
			childThreadId: THREAD, parentThreadId: 'parent-1',
			name: 'worker', provider: 'cc' as const, status: 'running' as const,
		}]]));
		sendMessage(THREAD, 'feed the worker');
		const frame = frames[0];

		await recoverPendingInputs(emptyHistory);

		expect(frames).toHaveLength(2);
		expect(frames[1]).toBe(frame);
		expect(JSON.parse(frames[1]).type).toBe('subagent_feed');
	});

	it('resends the pending inputs of a thread in the order they were sent', async () => {
		sendMessage(THREAD, 'first');
		sendMessage(THREAD, 'second');
		const sent = [...frames];

		await recoverPendingInputs(emptyHistory);

		expect(frames.slice(sent.length)).toEqual(sent);
	});

	it('resends nothing once the server history holds the row', async () => {
		const inputId = sendMessage(THREAD, 'made it')!;
		const fetchPage = vi.fn(async () => historyResponse([{
			id: inputId, role: 'user', content: 'made it', created_at: STORED_AT,
		}]));

		await recoverPendingInputs(fetchPage as typeof fetch);

		expect(fetchPage).toHaveBeenCalledOnce();
		expect(frames).toHaveLength(1);
		expect(getTurnState(THREAD).pendingInputIds).toEqual([]);
	});

	it.each([
		['a history read that throws', async (_url: RequestInfo | URL) => {
			throw new TypeError('offline');
		}],
		['a history read that is refused', async (_url: RequestInfo | URL) => (
			new Response('{}', { status: 503 })
		)],
		// Every page answers, so the read never reaches the end: absence here is
		// an unproved guess, not a finding.
		['a cursor the server repeats', async (_url: RequestInfo | URL) => (
			historyResponse([], true, 'cursor-1')
		)],
	])('resends nothing after %s', async (_label, fetchPage) => {
		sendMessage(THREAD, 'unknown to us');

		await recoverPendingInputs(fetchPage as typeof fetch);

		expect(frames).toHaveLength(1);
	});

	it('resends nothing when this socket no longer owns the connection', async () => {
		sendMessage(THREAD, 'lost with the old socket');

		await recoverPendingInputs(emptyHistory, () => false);

		expect(frames).toHaveLength(1);
	});

	it('resends nothing when ownership ends just before the resend', async () => {
		sendMessage(THREAD, 'lost with the old socket');
		// Ownership is held through every read and read-check, and lost at the
		// last possible moment: the check taken immediately before resending.
		let checksLeft = 7;
		const isCurrent = () => checksLeft-- > 0;
		const fetchPage = vi.fn(async (url: RequestInfo | URL) => (
			String(url).includes('before=')
				? historyResponse([])
				: historyResponse([], true, 'cursor-1')
		));

		await recoverPendingInputs(fetchPage as typeof fetch, isCurrent);

		expect(fetchPage).toHaveBeenCalledTimes(2);
		expect(frames).toHaveLength(1);
	});

	it('resends nothing it holds no frame for', async () => {
		// A page reload keeps the pending row and loses its frame with the socket.
		requestInput(THREAD, 'reloaded-input');

		await recoverPendingInputs(emptyHistory);

		expect(frames).toHaveLength(0);
		expect(getTurnState(THREAD).pendingInputIds).toEqual(['reloaded-input']);
	});
});

describe('the frame an input was sent with', () => {
	it('is forgotten once the input resolves', () => {
		const answered = sendMessage(THREAD, 'answered')!;
		const waiting = sendMessage(THREAD, 'still waiting')!;
		expect(_unconfirmedFrameIdsForTests()).toEqual([answered, waiting]);

		handleMessage({
			type: 'input_accepted', thread_id: THREAD, input_id: answered,
			message_id: 'row-1', created_at: STORED_AT, turn_id: 'turn-1', turn_order: 1,
		}, () => {});

		// The store is bounded by the inputs still pending, checked as each new
		// send records its own frame.
		const next = sendMessage(THREAD, 'next')!;
		expect(_unconfirmedFrameIdsForTests()).toEqual([waiting, next]);
	});
});
