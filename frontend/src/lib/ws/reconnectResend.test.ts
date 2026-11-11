import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import { subagentMeta } from '../stores/subagents';
import { chatReplyDrafts } from '../components/chatReplyDrafts.svelte';
import { activeThreadId, messages } from '../stores/threads';
import { handleMessage } from './handlers';
import { _unconfirmedFrameIdsForTests, resendUnconfirmedFrames, sendMessage } from './send';
import { setWs } from './state';
import { _resetTurnStateForTests, getTurnState, resetTurnState } from './turnState';

const THREAD = 'main-1';
const STORED_AT = '2026-11-05T18:00:00Z';

let frames: string[];

function mockSocket(): void {
	frames = [];
	setWs({
		readyState: WebSocket.OPEN,
		send: (data: string) => { frames.push(data); },
	} as unknown as WebSocket);
}

/** A new socket that records what is put on it. */
function newSocket(): { socket: WebSocket; sent: Record<string, unknown>[] } {
	const sent: Record<string, unknown>[] = [];
	const socket = { send: (data: string) => { sent.push(JSON.parse(data)); } } as unknown as WebSocket;
	return { socket, sent };
}

beforeEach(() => {
	mockSocket();
	subagentMeta.set(new Map());
	activeThreadId.set(THREAD);
	messages.set([]);
	_resetTurnStateForTests();
});

describe('resending what a dropped socket never confirmed', () => {
	it('resends a comment without a transcript row and clears its draft on a queued receipt', () => {
		const anchor = { from_off: 0, to_off: 1, quote: 'q', anchor_prefix: '', anchor_suffix: '' };
		const inputId = sendMessage(THREAD, '', undefined, undefined, undefined, 'apply_now', [{ id: 'c', seq: 1, ...anchor, note: 'keep' }])!;
		chatReplyDrafts.postRoot(THREAD, 'target', 'keep', inputId, anchor, 1);
		expect(get(messages)).toEqual([]);
		const { socket, sent } = newSocket();
		resendUnconfirmedFrames(socket);
		expect(sent).toEqual([JSON.parse(frames[0])]);
		handleMessage({ type: 'input_queued', queue_kind: 'messages', thread_id: THREAD, input_id: inputId, message_id: inputId, delivery_state: 'queued' }, () => {});
		expect(chatReplyDrafts.root(THREAD, 'target')).toBeUndefined();
		expect(_unconfirmedFrameIdsForTests()).toEqual([]);
	});

	it('puts every waiting frame on the new socket in send order, unchanged', () => {
		const first = sendMessage(THREAD, 'first')!;
		const other = sendMessage('main-2', 'another thread')!;
		const second = sendMessage(THREAD, 'second')!;
		const original = frames.map(frame => JSON.parse(frame));

		const { socket, sent } = newSocket();
		resendUnconfirmedFrames(socket);

		expect(sent.map(frame => frame.input_id)).toEqual([first, other, second]);
		expect(sent).toEqual(original);
	});

	it('resends a subagent feed the same way', () => {
		subagentMeta.set(new Map([['child-1', { status: 'idle' } as never]]));
		const inputId = sendMessage('child-1', 'work')!;

		const { socket, sent } = newSocket();
		resendUnconfirmedFrames(socket);

		expect(sent).toEqual([JSON.parse(frames[0])]);
		expect(sent[0]).toMatchObject({ type: 'subagent_feed', input_id: inputId });
	});

	it('resends nothing for an input the server already answered', () => {
		const answered = sendMessage(THREAD, 'answered')!;
		handleMessage({
			type: 'input_accepted', thread_id: THREAD, input_id: answered,
			message_id: 'row-1', created_at: STORED_AT, turn_id: 'turn-1', turn_order: 1,
		}, () => {});

		const { socket, sent } = newSocket();
		resendUnconfirmedFrames(socket);

		expect(sent).toEqual([]);
	});

	it('settles an input the server already stores from its duplicate receipt, in the stored state', () => {
		const inputId = sendMessage(THREAD, 'durable')!;

		handleMessage({
			type: 'input_queued', queue_kind: 'messages', thread_id: THREAD,
			input_id: inputId, message_id: inputId, queue_order: 1, created_at: STORED_AT,
			delivery_state: 'delivered', delivery_error: null, delivery_revision: 2,
			duplicate: true,
		}, () => {});

		expect(getTurnState(THREAD).pendingInputIds).toEqual([]);
		expect(get(messages)[0]).toMatchObject({
			id: inputId, created_at: STORED_AT, _optimistic: false, delivery_state: 'delivered',
		});
		expect(_unconfirmedFrameIdsForTests()).toEqual([]);
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

		const next = sendMessage(THREAD, 'next')!;
		expect(_unconfirmedFrameIdsForTests()).toEqual([waiting, next]);
	});

	it('is forgotten once the server accepts or rejects the input, with no further send', () => {
		const accepted = sendMessage(THREAD, 'accepted')!;
		const rejected = sendMessage(THREAD, 'rejected')!;
		const waiting = sendMessage(THREAD, 'still waiting')!;
		const sentFrames = [...frames];

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
		// frame where it stands, and nothing extra goes on the socket.
		expect(_unconfirmedFrameIdsForTests()).toEqual([waiting]);
		expect(frames).toEqual(sentFrames);
	});

	it('is forgotten once its thread is deleted', () => {
		sendMessage(THREAD, 'sent before the delete');

		// A delete resets the thread's turn state, and nothing will ask for its frames again.
		resetTurnState(THREAD);

		expect(_unconfirmedFrameIdsForTests()).toEqual([]);
	});
});
