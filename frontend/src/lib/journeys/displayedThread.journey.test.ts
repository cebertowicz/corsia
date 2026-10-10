/**
 * #1949 — a subagent conversation open in the artifact leaf beside its parent.
 *
 * The leaf claims its thread; from then on the same writers that serve the
 * active thread serve it (one writer per transition, no leaf branch), and each
 * surface shows only its own thread's rows. Frames go through `handleMessage`
 * and the echo through `sendMessage`, exactly as a real send walks them.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages, type Message } from '$lib/stores/threads';
import {
	_resetDisplayedThreadsForTests, adoptThreadSnapshot, claimDisplayedThread, retainDisplayedRows,
} from '$lib/stores/displayedThreads';
import { handleMessage } from '$lib/ws/handlers';
import { sendMessage } from '$lib/ws/send';
import { setWs } from '$lib/ws/state';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';
import QueuedInputStack from '$lib/components/QueuedInputStack.svelte';

const ACTIVE = 'parent-thread';
const LEAF = 'child-thread';
const CREATED = '2026-09-23T10:00:00.000000';

let host: HTMLElement;
let mounted: Record<string, unknown>[] = [];

function row(id: string, threadId: string, content = id): Message {
	return { id, thread_id: threadId, role: 'user', content, created_at: CREATED } as Message;
}

function rowsOf(threadId: string): Message[] {
	return get(messages).filter((m) => m.thread_id === threadId);
}

function chipsIn(el: HTMLElement): string[] {
	return [...el.querySelectorAll<HTMLElement>('.queued-chip .queued-text')]
		.map((chip) => chip.textContent?.trim() ?? '');
}

function frame(data: Record<string, unknown>): void {
	handleMessage(data, () => {});
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('token');
	activeThreadId.set(ACTIVE);
	messages.set([]);
	_resetTurnStateForTests();
	_resetDisplayedThreadsForTests();
	setWs({ readyState: 1, send: () => {} } as unknown as WebSocket);
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
	_resetDisplayedThreadsForTests();
});

describe('delivery to a claimed, non-active thread', () => {
	it('echoes, queues and delivers a leaf send on the leaf only', () => {
		claimDisplayedThread(LEAF);
		applyLifecycle(LEAF, { turnId: 'turn-1', turnOrder: 1 });
		const leafStack = document.createElement('div');
		const mainStack = document.createElement('div');
		host.append(leafStack, mainStack);
		mounted = [
			mount(QueuedInputStack, { target: leafStack, props: { threadId: LEAF } }),
			mount(QueuedInputStack, { target: mainStack }),
		];

		const inputId = sendMessage(LEAF, 'wait for me', undefined, undefined, undefined, 'queue_after_turn')!;
		flushSync();
		expect(rowsOf(LEAF)).toHaveLength(1);
		expect(chipsIn(leafStack)).toEqual(['wait for me']);
		expect(chipsIn(mainStack)).toEqual([]);

		frame({
			type: 'input_queued', queue_kind: 'messages', thread_id: LEAF, input_id: inputId, message_id: 'server-1',
			created_at: CREATED, delivery_state: 'queued', delivery_revision: 1,
		});
		expect(rowsOf(LEAF).map((m) => m.id)).toEqual(['server-1']);

		frame({
			type: 'message_added', thread_id: LEAF,
			message: {
				id: 'server-1', role: 'user', content: 'wait for me',
				metadata: { input_id: inputId, delivery_intent: 'queue_after_turn' },
				created_at: CREATED, delivery_state: 'delivered', delivery_revision: 2, seq: 3,
			},
		});
		expect(chipsIn(leafStack)).toEqual([]);
		expect(rowsOf(LEAF)).toHaveLength(1);
		expect(rowsOf(ACTIVE)).toEqual([]);
	});

	it('settles an accepted and a rejected leaf input exactly as for the active thread', () => {
		claimDisplayedThread(LEAF);
		const accepted = sendMessage(LEAF, 'go')!;
		const rejected = sendMessage(LEAF, 'nope')!;

		frame({
			type: 'input_accepted', thread_id: LEAF, input_id: accepted, message_id: 'server-go',
			created_at: CREATED, turn_id: 'turn-2', turn_order: 2,
		});
		frame({ type: 'error', thread_id: LEAF, input_id: rejected, message: 'Worker is closed' });

		const byContent = new Map(rowsOf(LEAF).map((m) => [m.content, m]));
		expect(byContent.get('go')).toMatchObject({ id: 'server-go', delivery_state: 'delivered' });
		expect(byContent.get('nope')).toMatchObject({
			delivery_state: 'failed', delivery_error: 'Worker is closed',
		});
	});

	it('writes nothing for a thread that no view displays', () => {
		sendMessage('elsewhere', 'hi');
		frame({
			type: 'message_added', thread_id: 'elsewhere',
			message: { id: 'm1', role: 'assistant', content: 'hello', created_at: CREATED },
		});
		expect(get(messages)).toEqual([]);
	});
});

describe('claims and snapshots', () => {
	it('drops a thread’s rows only on the last release', () => {
		const first = claimDisplayedThread(LEAF);
		const second = claimDisplayedThread(LEAF);
		messages.set([row('a', LEAF)]);

		first();
		first();
		expect(rowsOf(LEAF)).toHaveLength(1);

		second();
		expect(rowsOf(LEAF)).toEqual([]);
	});

	it('keeps the active thread’s rows when a leaf of the same thread releases', () => {
		const release = claimDisplayedThread(ACTIVE);
		messages.set([row('a', ACTIVE)]);
		release();
		expect(rowsOf(ACTIVE)).toHaveLength(1);
	});

	it('reconciles each displayed thread on reconnect without dropping the other', () => {
		claimDisplayedThread(LEAF);
		messages.set([row('stale', 'gone-thread'), row('p1', ACTIVE), row('c1', LEAF)]);

		adoptThreadSnapshot(LEAF, [row('c1', LEAF), row('c2', LEAF)]);
		adoptThreadSnapshot(ACTIVE, [row('p1', ACTIVE), row('p2', ACTIVE)]);

		expect(rowsOf(ACTIVE).map((m) => m.id)).toEqual(['p1', 'p2']);
		expect(rowsOf(LEAF).map((m) => m.id)).toEqual(['c1', 'c2']);
		expect(rowsOf('gone-thread')).toEqual([]);
	});

	it('keeps the leaf’s rows when the active thread’s first page fails', () => {
		claimDisplayedThread(LEAF);
		messages.set([row('old', 'previous'), row('c1', LEAF)]);

		retainDisplayedRows(ACTIVE);

		expect(get(messages).map((m) => m.id)).toEqual(['c1']);
	});
});
