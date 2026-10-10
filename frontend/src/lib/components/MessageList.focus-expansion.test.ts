import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { coreSettings } from '$lib/stores/settings';
import {
	activeThreadId, activeToolsByThread, messages, streamContentByThread, type Message,
} from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import {
	_resetTurnStateForTests, applyLifecycle, applyTerminal, requestInput,
} from '$lib/ws/turnState';
import { human, said, turn, work } from '$lib/__tests__/fixtures/focusRuns';
import { reactiveProps } from '$lib/__tests__/fixtures/reactiveProps.svelte';
import MessageList from './MessageList.svelte';

const A = 'thread-a';
const B = 'thread-b';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function renderList(extra: Record<string, unknown> = {}): Record<string, unknown> {
	const props = reactiveProps({
		messagesEl: undefined, userIsNearBottom: true, ignoreScrollEvents: false,
		onOpenLightbox: () => {}, ...extra,
	});
	component = mount(MessageList, { target: host, props });
	flushSync();
	return props;
}

const inThread = (threadId: string, rows: Record<string, any>[]): Message[] => (
	rows.map((row) => ({ ...row, thread_id: threadId }) as Message)
);
/** A thread whose loaded history starts with hidden work, then a reply. */
const startsWithRun = (threadId: string) => inThread(threadId, [
	work(`${threadId}-step`, 1, '10:01:00'), said(`${threadId}-reply`, 2, '10:02:00'),
]);
const toggles = () => [...host.querySelectorAll<HTMLButtonElement>('.messages > .transcript-fold > .fold-toggle')];
const open = () => toggles().map((toggle) => toggle.getAttribute('aria-expanded'));
const faces = () => toggles().map((toggle) => toggle.textContent?.replace('▸', '').trim());
const frame = (data: Record<string, unknown>) => { handleMessage(data, () => {}); flushSync(); };

/** What `sendMessage` writes before any server frame: the echo, and the requested input. */
function sendEcho(threadId: string, inputId: string): void {
	messages.update((rows) => [...rows, {
		id: inputId, thread_id: threadId, role: 'user', content: 'steer',
		metadata: { input_id: inputId }, created_at: '2026-10-02T10:05:00.000Z', _optimistic: true,
	}]);
	requestInput(threadId, inputId);
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(A);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	streamContentByThread.set(new Map());
	messages.set([]);
	coreSettings.set({ features: { focus_mode: true } } as any);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	streamContentByThread.set(new Map());
	messages.set([]);
	coreSettings.set(null);
});

describe('Focus run expansion belongs to one thread', () => {
	it('leaves the next thread\'s first run closed after the first thread\'s was opened', () => {
		messages.set([...startsWithRun(A), ...startsWithRun(B)]);
		renderList();
		toggles()[0].click();
		flushSync();
		expect(open()).toEqual(['true']);

		activeThreadId.set(B);
		flushSync();
		expect(host.querySelector(`[data-message-id="${B}-reply"]`)).not.toBeNull();
		expect(open()).toEqual(['false']);
	});

	it('starts closed again on returning to the thread', () => {
		messages.set([...startsWithRun(A), ...startsWithRun(B)]);
		renderList();
		toggles()[0].click();
		flushSync();

		activeThreadId.set(B);
		flushSync();
		activeThreadId.set(A);
		flushSync();
		expect(open()).toEqual(['false']);
	});

	it('keeps a run open through older rows, newer rows and a refetch of the same thread', () => {
		messages.set(startsWithRun(A));
		const props = renderList({ hasMoreMessages: true });
		toggles()[0].click();
		flushSync();

		const older = inThread(A, [work('older-step', 0, '10:00:30')]);
		const newer = inThread(A, [work('newer-step', 3, '10:03:00', 't2')]);
		messages.set([...older, ...startsWithRun(A), ...newer]);
		props.hasMoreMessages = false;
		flushSync();
		expect(open()).toEqual(['true', 'false']);

		messages.set(inThread(A, [...older, ...startsWithRun(A), ...newer]));
		flushSync();
		expect(open()).toEqual(['true', 'false']);
	});
});

describe('Focus runs when the first page failed', () => {
	const rows = () => inThread(A, [work('step', 1, '10:01:00'), said('reply', 2, '10:02:00')]);
	const turnSummaries = { t1: turn('10:00:00', '10:02:00', 1) };

	it('counts a first run whose start is known', () => {
		messages.set(rows());
		renderList({ turnSummaries });

		expect(faces()).toEqual(['Worked · 1 step · 2 min']);
	});

	it('treats history as unknown, so a first run over retained rows claims no count', () => {
		messages.set(rows());
		renderList({ turnSummaries, firstLoadFailed: true });

		expect(faces()).toEqual(['Worked · 1+ steps']);
	});
});

describe('Focus run expansion through a send, in the order frames arrive', () => {
	const accepted = (inputId: string, turnId: string, turnOrder: number, seq: number) => frame({
		type: 'input_accepted', thread_id: A, input_id: inputId, message_id: `m-${inputId}`,
		created_at: '2026-10-02T10:05:00', turn_id: turnId, turn_order: turnOrder, seq, delivery_revision: 1,
	});

	it('W9: a run opened before it holds a row stays open through its rows and its reply', () => {
		messages.set(inThread(A, [human('ask', 1, '10:00:00', 't0', '10:00:00'), said('answer', 2, '10:01:00', 't0')]));
		renderList();
		sendEcho(A, 'X');
		// Requested, not running: the echo reads in the transcript and no run exists yet.
		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(toggles()).toHaveLength(0);

		accepted('X', 't1', 1, 3);
		expect(open()).toEqual(['false']);
		toggles()[0].click();
		flushSync();
		expect(open()).toEqual(['true']);

		frame({ type: 'message_added', thread_id: A, message: { ...work('s1', 4, '10:06:00'), thread_id: A } });
		expect(open()).toEqual(['true']);
		frame({ type: 'message_added', thread_id: A, message: { ...said('reply', 5, '10:07:00'), thread_id: A } });
		applyTerminal(A, { turnId: 't1', turnOrder: 1 });
		flushSync();
		expect(open()).toEqual(['true']);
		expect(host.querySelector('.live-work')).toBeNull();
	});

	/** A steer into a running Turn: echo, queued receipt, then the Turn claims it in place. */
	function steer(seq: number): void {
		sendEcho(A, 'S');
		expect(host.querySelector('.pending-input-region [data-message-id="S"]')).not.toBeNull();
		frame({
			type: 'input_queued', queue_kind: 'messages', thread_id: A, input_id: 'S',
			message_id: 'm-S', created_at: '2026-10-02T10:05:00', delivery_revision: 1,
		});
		frame({
			type: 'message_added', thread_id: A,
			message: {
				id: 'm-S', thread_id: A, role: 'user', content: 'steer', seq,
				created_at: '2026-10-02T10:05:00', metadata: { input_id: 'S' },
				delivery_state: 'queued', delivery_revision: 2, active_turn_id: 't1',
			},
		});
		expect(host.querySelector('.pending-input-region')).toBeNull();
	}

	it('keeps the work before a steer open; the work after it is a new, closed run', () => {
		messages.set(inThread(A, [human('ask', 1, '10:00:00', 't1', '10:00:00'), work('s1', 2, '10:01:00')]));
		applyLifecycle(A, { turnId: 't1', turnOrder: 1 });
		renderList();
		toggles()[0].click();
		flushSync();
		expect(open()).toEqual(['true']);

		steer(3);
		expect(open()).toEqual(['true', 'false']);
		expect(toggles()[1].closest('.transcript-fold')?.classList.contains('live-work')).toBe(true);
	});

	// The split rule applies here too (Product Experience disposition, 2026-10-02): design 3.1 keeps an
	// empty open run's `after:` token "because the row before the run does not change", and a steer
	// changes it. The opened run ends empty; the run after the steer is a different, closed one.
	it('an empty open run opened before a steer does not carry past the steer', () => {
		messages.set(inThread(A, [human('ask', 1, '10:00:00', 't1', '10:00:00')]));
		applyLifecycle(A, { turnId: 't1', turnOrder: 1 });
		renderList();
		toggles()[0].click();
		flushSync();
		expect(open()).toEqual(['true']);

		steer(2);
		expect(open()).toEqual(['false']);
	});
});
