/**
 * JOURNEY J3 (client) — hold to queue a message, then watch the queue drain.
 *
 * THE DEFECT THIS ENCODES. A unit test proved a queued chip RENDERS. Nothing
 * proved it DISAPPEARS, and it did not: chips sat on the composer after the
 * message was already in the transcript. Rendering is one step; un-rendering
 * is the step past it, and it is the step the user actually notices.
 *
 * So this test does not poke the store. It walks the real client pipeline the
 * way a real send walks it — `sendMessage` writes the optimistic echo, the
 * server's own frames arrive through `handleMessage` — and asks the two
 * surfaces what they show at each moment. The frames below are transcribed
 * from a live isolated backend (`backend/tests/test_user_journeys.py::
 * test_j3_a_queued_input_holds_no_place_until_it_earns_one`), so the client is
 * being fed what the server actually sends, not what this file wishes it sent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import { sendMessage } from '$lib/ws/send';
import { setWs } from '$lib/ws/state';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';
import MessageList from '$lib/components/MessageList.svelte';
import QueuedInputStack from '$lib/components/QueuedInputStack.svelte';

const THREAD_ID = 'journey-thread';
const TURN = { turnId: 'turn-1', turnOrder: 1 };
const SERVER_ID = '482b0259-ec2f-52f0-9521-04633554848e';
const QUEUED_TEXT = 'the queued thing';

let host: HTMLElement;
let mounted: Record<string, unknown>[] = [];
let sent: any[] = [];

function fakeSocket(): void {
	sent = [];
	setWs({
		readyState: 1,
		send: (raw: string) => sent.push(JSON.parse(raw)),
	} as unknown as WebSocket);
}

function render(): void {
	mounted = [
		mount(QueuedInputStack, { target: host }),
		mount(MessageList, {
			target: host,
			props: {
				messagesEl: undefined, userIsNearBottom: true, ignoreScrollEvents: false,
				onOpenLightbox: () => {},
			},
		}),
	];
	flushSync();
}

/** What the composer's queue shows right now. */
function chips(): string[] {
	return [...host.querySelectorAll<HTMLElement>('.queued-chip .queued-text')]
		.map((el) => el.textContent?.trim() ?? '');
}

/** What the transcript shows right now, in the order it reads. */
function transcript(): string[] {
	return [...host.querySelectorAll<HTMLElement>('[data-message-id] .bubble-content')]
		.map((el) => el.textContent?.trim() ?? '');
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('token');
	activeThreadId.set(THREAD_ID);
	messages.set([]);
	_resetTurnStateForTests();
	fakeSocket();
});

afterEach(() => {
	for (const component of mounted) unmount(component);
	mounted = [];
	host.remove();
	setWs(null);
	token.set(null);
	activeThreadId.set(null);
	messages.set([]);
	_resetTurnStateForTests();
	vi.unstubAllGlobals();
});

describe('J3 — a message queued behind a running turn', () => {
	it('appears on the composer, never in the transcript, and LEAVES on delivery', () => {
		applyLifecycle(THREAD_ID, TURN);
		render();

		// 3.1 — the user holds Send during a running turn.
		const inputId = sendMessage(
			THREAD_ID, QUEUED_TEXT, undefined, undefined, undefined, 'queue_after_turn',
		)!;
		flushSync();

		// OBS — it is visibly waiting, on the surface the user is holding, and
		// it has NOT taken a place among messages that were actually sent.
		expect(chips()).toEqual([QUEUED_TEXT]);
		expect(transcript()).not.toContain(QUEUED_TEXT);
		expect(sent.at(-1)).toMatchObject({
			type: 'message', delivery_intent: 'queue_after_turn',
		});

		// 3.2 — the server acknowledges the queue with its own row id.
		handleMessage({
			type: 'input_queued', queue_kind: 'messages', thread_id: THREAD_ID,
			input_id: inputId, message_id: SERVER_ID, queue_order: 2,
			created_at: '2026-08-10T05:15:40.629857', delivery_state: 'queued',
			delivery_error: null, delivery_revision: 1, duplicate: false,
		}, () => {});
		flushSync();

		// Still waiting — and still exactly one of it. The receipt swapped the
		// optimistic row's identity; it did not add a second copy (A5).
		expect(chips()).toEqual([QUEUED_TEXT]);
		expect(get(messages).filter((m) => m.content === QUEUED_TEXT)).toHaveLength(1);
		expect(get(messages)[0].id).toBe(SERVER_ID);
		expect(host.querySelector<HTMLButtonElement>('.queued-remove')!.disabled).toBe(false);

		// 3.3 — the turn ends and the queue drains. THE STEP PAST (P1).
		handleMessage({
			type: 'message_added', thread_id: THREAD_ID,
			message: {
				id: SERVER_ID, role: 'user', content: QUEUED_TEXT,
				metadata: { input_id: inputId, delivery_intent: 'queue_after_turn' },
				created_at: '2026-08-10T05:15:40.629857',
				delivery_state: 'delivered', delivery_error: null,
				delivery_revision: 2, seq: 3,
			},
		}, () => {});
		flushSync();

		expect(chips()).toEqual([]);
		expect(host.querySelector('.queued-stack')).toBeNull();
		// It did not merely vanish — it arrived. A chip that disappears with no
		// bubble to show for it is the same lie in the other direction.
		expect(transcript()).toEqual([QUEUED_TEXT]);
		expect(get(messages).filter((m) => m.content === QUEUED_TEXT)).toHaveLength(1);
	});

	it('stays gone across the next turn, its answer, and a snapshot (P2)', () => {
		applyLifecycle(THREAD_ID, TURN);
		render();
		const inputId = sendMessage(
			THREAD_ID, QUEUED_TEXT, undefined, undefined, undefined, 'queue_after_turn',
		)!;
		handleMessage({
			type: 'input_queued', thread_id: THREAD_ID, input_id: inputId,
			message_id: SERVER_ID, queue_order: 2, delivery_revision: 1,
			created_at: '2026-08-10T05:15:40.629857', delivery_state: 'queued',
		}, () => {});
		handleMessage({
			type: 'message_added', thread_id: THREAD_ID,
			message: {
				id: SERVER_ID, role: 'user', content: QUEUED_TEXT,
				metadata: { input_id: inputId, delivery_intent: 'queue_after_turn' },
				created_at: '2026-08-10T05:15:40.629857', delivery_state: 'delivered',
				delivery_error: null, delivery_revision: 2, seq: 3,
			},
		}, () => {});
		flushSync();
		expect(host.querySelector('.queued-stack')).toBeNull();

		// Everything that follows a delivery: the answer, the turn ending, and a
		// fresh snapshot on reconnect. "Gone" that comes back on the next store
		// update is the defect wearing a delay.
		handleMessage({
			type: 'message_added', thread_id: THREAD_ID,
			message: {
				id: 'assistant-1', role: 'assistant', content: 'SECOND_ANSWER',
				metadata: {}, created_at: '2026-08-10T05:15:44.7', seq: 4,
			},
		}, () => {});
		handleMessage({ type: 'turn_complete', thread_id: THREAD_ID, ...frameTurn() }, () => {});
		handleMessage({
			type: 'session_snapshot', thread_id: THREAD_ID,
			turn_id: null, turn_order: null, state: null,
		}, () => {});
		flushSync();

		expect(host.querySelector('.queued-stack')).toBeNull();
		expect(transcript()).toEqual([QUEUED_TEXT]);
		expect(get(messages).filter((m) => m.content === QUEUED_TEXT)).toHaveLength(1);
	});

	it('shows and then releases the chip on a surface that did not send it (P7)', () => {
		// The other device. It has no optimistic echo and no `input_id` of its
		// own — it only ever receives the broadcast rows, which is why the
		// correlation path cannot be what makes the chip leave. The backend
		// journey `test_j9_…` proves the server sends both rows; this proves the
		// second surface does the right thing with them.
		applyLifecycle(THREAD_ID, TURN);
		render();

		handleMessage({
			type: 'message_added', thread_id: THREAD_ID,
			message: {
				id: SERVER_ID, role: 'user', content: QUEUED_TEXT,
				metadata: { input_id: 'sent-from-the-phone', delivery_intent: 'queue_after_turn' },
				created_at: '2026-08-10T05:15:40.629857', delivery_state: 'queued',
				delivery_error: null, delivery_revision: 1, seq: null,
			},
		}, () => {});
		flushSync();
		expect(chips()).toEqual([QUEUED_TEXT]);
		expect(transcript()).not.toContain(QUEUED_TEXT);

		handleMessage({
			type: 'message_added', thread_id: THREAD_ID,
			message: {
				id: SERVER_ID, role: 'user', content: QUEUED_TEXT,
				metadata: { input_id: 'sent-from-the-phone', delivery_intent: 'queue_after_turn' },
				created_at: '2026-08-10T05:15:40.629857', delivery_state: 'delivered',
				delivery_error: null, delivery_revision: 2, seq: 3,
			},
		}, () => {});
		flushSync();

		expect(host.querySelector('.queued-stack')).toBeNull();
		expect(transcript()).toEqual([QUEUED_TEXT]);
		expect(get(messages).filter((m) => m.content === QUEUED_TEXT)).toHaveLength(1);
	});

	it('lets go of a queue that ends by cancellation, not only by delivery', () => {
		// Both endings retire the chip. Only delivery was ever checked, which is
		// how "it disappears when it is delivered" hid "it never disappears
		// otherwise" — a cancelled message would keep its chip AND take a place
		// in the transcript, so the user would see it twice.
		applyLifecycle(THREAD_ID, TURN);
		render();
		const inputId = sendMessage(
			THREAD_ID, QUEUED_TEXT, undefined, undefined, undefined, 'queue_after_turn',
		)!;
		handleMessage({
			type: 'input_queued', thread_id: THREAD_ID, input_id: inputId,
			message_id: SERVER_ID, queue_order: 2, delivery_revision: 1,
			created_at: '2026-08-10T05:15:40.629857', delivery_state: 'queued',
		}, () => {});
		flushSync();
		expect(chips()).toEqual([QUEUED_TEXT]);

		handleMessage({
			type: 'message_added', thread_id: THREAD_ID,
			message: {
				id: SERVER_ID, role: 'user', content: QUEUED_TEXT,
				metadata: { input_id: inputId, delivery_intent: 'queue_after_turn' },
				created_at: '2026-08-10T05:15:40.629857',
				delivery_state: 'cancelled', delivery_error: 'cancelled_by_user',
				delivery_revision: 2, seq: 1,
			},
		}, () => {});
		flushSync();

		expect(host.querySelector('.queued-stack')).toBeNull();
		expect(transcript()).toEqual([QUEUED_TEXT]);
		expect(host.querySelector('.delivery-status')!.textContent).toBe('Cancelled by you');
	});
});

function frameTurn(): Record<string, unknown> {
	return { turn_id: TURN.turnId, turn_order: TURN.turnOrder };
}
