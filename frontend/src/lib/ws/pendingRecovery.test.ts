import { beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from '../stores/auth';
import { subagentMeta } from '../stores/subagents';
import { activeThreadId, messages } from '../stores/threads';
import { handleMessage } from './handlers';
import { recoverPendingInputs } from './pendingRecovery';
import { _forgetUnconfirmedFramesForTests, _unconfirmedFrameIdsForTests, sendMessage } from './send';
import { setWs } from './state';
import {
	_resetTurnStateForTests, getTurnState, requestInput, resetTurnState, restoreTurnState,
} from './turnState';

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

/** A history read a test settles by hand, so a send can land while it is open. */
function deferredHistory() {
	let settle: (page: Response) => void = () => {};
	let refuse: (error: Error) => void = () => {};
	const page = new Promise<Response>((resolve, reject) => {
		settle = resolve;
		refuse = reject;
	});
	return {
		fetchPage: (((() => page) as unknown) as typeof fetch),
		settle,
		refuse,
	};
}

beforeEach(() => {
	mockSocket();
	subagentMeta.set(new Map());
	activeThreadId.set(THREAD);
	messages.set([]);
	_resetTurnStateForTests();
	_forgetUnconfirmedFramesForTests();
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
		// Settling the row settled its frame too: recovery does not keep a copy
		// of an input the server has.
		expect(_unconfirmedFrameIdsForTests()).toEqual([]);
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
		// A pending input this client holds no frame for: the read proves it is
		// absent, and there is nothing here to put back on the socket.
		requestInput(THREAD, 'reloaded-input');

		await recoverPendingInputs(emptyHistory);

		expect(frames).toHaveLength(0);
		expect(getTurnState(THREAD).pendingInputIds).toEqual(['reloaded-input']);
	});

	it('sends a message typed during the read only after the lost input it must follow', async () => {
		// The first input went on the socket that died, so its frame is the one
		// recovery owes the server, and it was asked for before anything this
		// socket's person sends.
		const lostId = sendMessage(THREAD, 'lost with the old socket')!;
		const lostFrame = frames[0];
		mockSocket();
		const paging = deferredHistory();

		const recovering = recoverPendingInputs(paging.fetchPage);
		const laterId = sendMessage(THREAD, 'typed while the read is open')!;
		// Held back: the row is queued and its frame is kept, and the new socket
		// carries nothing while the read that decides the order is still open.
		expect(frames).toHaveLength(0);

		paging.settle(historyResponse([]));
		await recovering;

		expect(frames).toHaveLength(2);
		expect(frames[0]).toBe(lostFrame);
		expect(frames.map(frame => JSON.parse(frame).input_id)).toEqual([lostId, laterId]);
		expect(JSON.parse(frames[1])).toMatchObject({ content: 'typed while the read is open' });
	});

	it('sends a held message once its own thread is read, without waiting on another thread', async () => {
		const OTHER = 'main-2';
		sendMessage(THREAD, 'lost here');
		sendMessage(OTHER, 'lost there');
		mockSocket();
		const slow = deferredHistory();
		const fetchPage = ((url: string) => (
			url.includes(`/threads/${THREAD}/`) ? slow.fetchPage(url) : emptyHistory()
		)) as unknown as typeof fetch;

		const recovering = recoverPendingInputs(fetchPage);
		const laterId = sendMessage(OTHER, 'typed while the other thread is read')!;
		try {
			await vi.waitFor(() => {
				expect(frames.map(frame => JSON.parse(frame).input_id)).toContain(laterId);
			});
			expect(frames.every(frame => JSON.parse(frame).thread_id !== THREAD)).toBe(true);
		} finally {
			slow.settle(historyResponse([]));
			await recovering;
		}
	});

	it('still sends a held message when the read fails, and resends nothing unproved', async () => {
		const lostId = sendMessage(THREAD, 'lost with the old socket')!;
		const lostFrame = frames[0];
		mockSocket();
		const paging = deferredHistory();

		const recovering = recoverPendingInputs(paging.fetchPage);
		const laterId = sendMessage(THREAD, 'typed while the read is open')!;
		expect(frames).toHaveLength(0);

		paging.refuse(new TypeError('offline'));
		await recovering;

		// The lost frame's absence was never proved, so it stays off the wire. The
		// held frame never went on it at all, and leaving it there would hang the
		// person's own message on a read that already failed.
		expect(frames).toHaveLength(1);
		expect(frames).not.toContain(lostFrame);
		expect(JSON.parse(frames[0]).input_id).toBe(laterId);
		expect(getTurnState(THREAD).pendingInputIds).toEqual([lostId, laterId]);
	});

	it('records why a thread\'s recovery threw, and still recovers the other thread', async () => {
		const OTHER = 'main-2';
		sendMessage(THREAD, 'lost here');
		sendMessage(OTHER, 'lost there');
		mockSocket();
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const fetchPage = (async (url: string) => (
			url.includes(`/threads/${THREAD}/`)
				? new Response('not json', { status: 200 })
				: historyResponse([])
		)) as unknown as typeof fetch;

		try {
			await recoverPendingInputs(fetchPage);

			expect(warn).toHaveBeenCalledOnce();
			expect(warn.mock.calls[0][1]).toBe(THREAD);
			expect(warn.mock.calls[0][2]).toBeInstanceOf(SyntaxError);
			// The other thread's read finished, so its lost frame went back on.
			expect(frames.map(frame => JSON.parse(frame).thread_id)).toEqual([OTHER]);
		} finally {
			warn.mockRestore();
		}
	});

	it('sends nothing on a socket a newer one replaced, and the new recovery sends in order', async () => {
		const lostId = sendMessage(THREAD, 'lost with the old socket')!;
		const lostFrame = frames[0];
		mockSocket();
		let owns = true;
		const paging = deferredHistory();

		const recovering = recoverPendingInputs(paging.fetchPage, () => owns);
		const laterId = sendMessage(THREAD, 'typed while the read is open')!;
		expect(frames).toHaveLength(0);

		// Ownership ends mid-read, and the read then answers anyway: a retired
		// socket gets nothing sent on it, and both frames stay pending with it.
		owns = false;
		mockSocket();
		paging.settle(historyResponse([]));
		await recovering;

		expect(frames).toHaveLength(0);

		// The new socket's own recovery proves both absent and puts them on in the
		// order they were asked for.
		await recoverPendingInputs(emptyHistory);

		expect(frames).toHaveLength(2);
		expect(frames[0]).toBe(lostFrame);
		expect(frames.map(frame => JSON.parse(frame).input_id)).toEqual([lostId, laterId]);
	});

	it('sends at once on a thread the new recovery does not read, though a replaced run still reads it', async () => {
		const settledId = sendMessage(THREAD, 'answered while the socket changed')!;
		mockSocket();
		let owns = true;
		const paging = deferredHistory();
		const replaced = recoverPendingInputs(paging.fetchPage, () => owns);

		// The socket retires mid-read, and the thread's only pending input settles
		// before the new socket's recovery looks, so that recovery holds nothing.
		owns = false;
		mockSocket();
		handleMessage({
			type: 'input_accepted', thread_id: THREAD, input_id: settledId,
			message_id: 'row-1', created_at: STORED_AT, turn_id: 'turn-1', turn_order: 1,
		}, () => {});
		await recoverPendingInputs(emptyHistory);

		try {
			const laterId = sendMessage(THREAD, 'typed on the new socket')!;
			expect(frames.map(frame => JSON.parse(frame).input_id)).toEqual([laterId]);
		} finally {
			paging.settle(historyResponse([]));
			await replaced;
		}
		expect(frames).toHaveLength(1);
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

		// The store is bounded by the inputs still pending: turn state's own
		// membership settles it, and this send only adds its own frame.
		const next = sendMessage(THREAD, 'next')!;
		expect(_unconfirmedFrameIdsForTests()).toEqual([waiting, next]);
	});

	it('is forgotten once the server accepts or rejects the input, with no further send', () => {
		const accepted = sendMessage(THREAD, 'accepted')!;
		const rejected = sendMessage(THREAD, 'rejected')!;
		const waiting = sendMessage(THREAD, 'still waiting')!;
		const sentFrames = [...frames];
		expect(_unconfirmedFrameIdsForTests()).toEqual([accepted, rejected, waiting]);

		handleMessage({
			type: 'input_accepted', thread_id: THREAD, input_id: accepted,
			message_id: 'row-1', created_at: STORED_AT, turn_id: 'turn-1', turn_order: 1,
		}, () => {});
		expect(_unconfirmedFrameIdsForTests()).toEqual([rejected, waiting]);

		handleMessage({
			type: 'error', thread_id: THREAD, input_id: rejected,
			message: 'Message was not delivered',
		}, () => {});

		// Turn state is the one owner of membership: settling an input drops its
		// frame where it stands, with no next send to trigger a prune and nothing
		// extra put on the socket.
		expect(_unconfirmedFrameIdsForTests()).toEqual([waiting]);
		expect(frames).toEqual(sentFrames);
	});

	it('is kept through a failed delete, so recovery can still resend it', async () => {
		const inputId = sendMessage(THREAD, 'sent before the delete')!;
		const frame = frames[0];

		// A delete resets the thread, then puts its turn state back when the delete fails.
		const prior = getTurnState(THREAD);
		resetTurnState(THREAD);
		restoreTurnState(THREAD, prior);
		await recoverPendingInputs(emptyHistory);

		expect(frames).toEqual([frame, frame]);
		expect(getTurnState(THREAD).pendingInputIds).toEqual([inputId]);
	});
});
