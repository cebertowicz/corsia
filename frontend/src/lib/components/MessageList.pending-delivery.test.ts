import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { token } from '$lib/stores/auth';
import { coreSettings } from '$lib/stores/settings';
import {
	activeThreadId,
	activeToolsByThread,
	messages,
} from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import { _resetTurnStateForTests, applyLifecycle, applyTerminal } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

const THREAD_ID = 'pending-delivery-session';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function rowIds(scope: ParentNode = host): string[] {
	return [...scope.querySelectorAll<HTMLElement>('[data-message-id]')]
		.map((row) => row.dataset.messageId ?? '');
}

function renderList(): void {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	messages.set([]);
	coreSettings.set({ features: { focus_mode: false } } as any);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	messages.set([]);
	coreSettings.set(null);
	token.set(null);
});

describe('MessageList provider-pending apply-now region', () => {
	describe('in Focus', () => {
		beforeEach(() => {
			coreSettings.set({ features: { focus_mode: true } } as any);
		});

		it('shows a waiting human message', () => {
			messages.set([pendingInput({ id: 'waiting-human' })]);
			renderList();

			expect(rowIds(host.querySelector('.pending-input-region')!)).toEqual(['waiting-human']);
		});

		it('hides a waiting agent send', () => {
			messages.set([pendingInput({ id: 'waiting-agent-send', metadata: { sender_kind: 'agent' } })]);
			renderList();

			expect(host.querySelector('.pending-input-region')).toBeNull();
			expect(host.querySelector('[data-message-id="waiting-agent-send"]')).toBeNull();
		});

		it('hides a waiting plan or system notice', () => {
			messages.set([{
				id: 'waiting-plan-notice', thread_id: THREAD_ID, role: 'system',
				content: '[EVENT: plan.changed]', created_at: '2026-10-11T10:00:00Z',
				metadata: { type: 'context_event', event_type: 'plan.changed', event_disposition: 'passive' },
				delivery_state: 'queued', delivery_revision: 1,
			}]);
			renderList();

			expect(host.querySelector('.pending-input-region')).toBeNull();
			expect(host.querySelector('[data-message-id="waiting-plan-notice"]')).toBeNull();
		});

		it('shows a retry wake as one retry line', () => {
			messages.set([{
				id: 'retry-wake', thread_id: THREAD_ID, role: 'system',
				content: 'Retry this wake', created_at: '2026-10-11T10:00:00Z',
				metadata: { type: 'context_event', event_disposition: 'wake', summary: 'Retry this wake' },
				delivery_state: 'failed', delivery_revision: 1,
			}]);
			renderList();

			expect(host.querySelectorAll('[data-wake-retry]')).toHaveLength(1);
			expect(host.querySelectorAll('[data-message-id="retry-wake"]')).toHaveLength(0);
		});
	});

	it('keeps waiting agent sends in the pending region when Focus is off', () => {
		messages.set([pendingInput({ id: 'waiting-agent-send', metadata: { sender_kind: 'agent' } })]);
		renderList();

		expect(rowIds(host.querySelector('.pending-input-region')!)).toEqual(['waiting-agent-send']);
	});

	it('moves an unreceipted optimistic echo into the transcript when its turn ends', () => {
		messages.set([
			{
				id: 'assistant-current-step', thread_id: THREAD_ID, role: 'assistant',
				content: 'reply in progress', created_at: '2026-09-25T08:00:00Z', seq: 40,
			},
			{
				id: 'optimistic-echo', thread_id: THREAD_ID, role: 'user',
				content: 'apply this now', created_at: '2026-09-25T08:00:01Z', _optimistic: true,
			},
		]);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		renderList();

		expect(rowIds(host.querySelector('.pending-input-region')!)).toEqual(['optimistic-echo']);

		applyTerminal(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 }, false);
		flushSync();

		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(rowIds()).toEqual(['assistant-current-step', 'optimistic-echo']);
	});

	it('aggregates passive context events below the transcript until delivery', () => {
		const pending = [1, 2, 3].map((index) => ({
			id: `context-${index}`, thread_id: THREAD_ID, role: 'system',
			content: `[EVENT: plan.changed] ${index}`,
			created_at: `2026-08-28T12:0${index}:00Z`,
			metadata: {
				type: 'context_event', event_type: 'plan.changed',
				event_disposition: 'passive', summary: `Plan change ${index}`,
			},
			delivery_state: 'queued', delivery_revision: 1, input_order: index,
		}));
		messages.set([
			{
				id: 'delivered-before', thread_id: THREAD_ID, role: 'assistant',
				content: 'existing history', created_at: '2026-08-28T12:00:00Z', seq: 8,
			},
			...pending,
		]);

		renderList();

		const region = host.querySelector<HTMLElement>('.pending-input-region');
		expect(region).not.toBeNull();
		expect(region?.querySelectorAll('[data-context-digest="pending"]')).toHaveLength(1);
		expect(region?.querySelector('.ev-summary')?.textContent).toBe('3 context updates');
		expect(rowIds()).toEqual(['delivered-before', 'pending-context-digest']);

		handleMessage({
			type: 'messages_added', thread_id: THREAD_ID,
			messages: pending.map((message, index) => ({
				...message, delivery_state: 'delivered', delivery_revision: 2, seq: 9 + index,
			})),
		}, () => {});
		flushSync();

		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(host.querySelectorAll('[data-context-digest="delivered"]')).toHaveLength(1);
		expect(rowIds()).toEqual(['delivered-before', 'context-digest-context-1-context-3']);
	});

	it('preserves one backend digest and its source-event details while pending', () => {
		messages.set([{
			id: 'context-digest:session:1:2', thread_id: THREAD_ID, role: 'system',
			content: '[PASSIVE CONTEXT DIGEST]\n2 context updates',
			created_at: '2026-08-28T12:02:00Z',
			metadata: {
				type: 'context_event', event_type: 'context.digest',
				event_disposition: 'passive', summary: '2 context updates',
				context_events: [
					{ id: 'one', summary: 'Plan paused.', created_at: '2026-08-28T12:00:00Z' },
					{ id: 'two', summary: 'Artifact changed.', created_at: '2026-08-28T12:01:00Z' },
				],
			},
			delivery_state: 'queued', delivery_revision: 1, input_order: 1,
		}]);

		renderList();

		expect(rowIds()).toEqual(['context-digest:session:1:2']);
		host.querySelector<HTMLButtonElement>('[data-event-disclosure]')?.click();
		flushSync();
		expect(host.querySelectorAll('.ev-detail')).toHaveLength(2);
		expect(host.textContent).toContain('Artifact changed.');
	});

	it('keeps the live indicator below every canonical pending row', () => {
		messages.set([
			{
				id: 'pending-input', thread_id: THREAD_ID, role: 'user',
				content: 'hold until safe', created_at: '2026-07-28T12:00:00Z',
				metadata: { input_id: 'input-1' },
				delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
			},
			{
				id: 'assistant-text', thread_id: THREAD_ID, role: 'assistant',
				content: 'working above the pending input', created_at: '2026-07-28T12:00:01Z',
			},
			{
				id: 'tool-result', thread_id: THREAD_ID, role: 'assistant',
				content: 'tool result remains above the pending input', created_at: '2026-07-28T12:00:02Z',
				metadata: { tool_hints: ['bounded test'] },
			},
		]);
		// Live tool output only exists inside a live turn, and the live region
		// renders only while one is running — a spinner must not outlive its turn.
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		activeToolsByThread.set(new Map([[THREAD_ID, [{
			id: 'tool-progress', name: 'test-tool', hint: 'in progress', status: 'running',
		}]]]));

		renderList();

		const pendingRegion = host.querySelector<HTMLElement>('.pending-input-region');
		expect(pendingRegion).not.toBeNull();
		expect(rowIds(pendingRegion!)).toEqual(['pending-input']);
		expect(rowIds()).toEqual(['assistant-text', 'tool-result', 'pending-input']);

		// The live indicator is OUTPUT — the current step speaking — so an
		// not-yet-delivered input renders below it, not above. This reverses the
		// assertion #895 made (pending above the indicator); the product owner
		// corrected it on 2026-08-09: "anything not delivered needs to go under
		// the actual output... that includes under the typing indicator."
		const liveTool = host.querySelector<HTMLElement>('.tool-activity');
		expect(liveTool).not.toBeNull();
		expect(liveTool!.compareDocumentPosition(pendingRegion!) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBe(Node.DOCUMENT_POSITION_FOLLOWING);
	});

	it('reactively settles an input once the active turn claims it', () => {
		const input = {
			id: 'active-input', thread_id: THREAD_ID, role: 'user',
			content: 'already sent to this turn', created_at: '2026-07-28T12:00:00Z',
			metadata: { input_id: 'input-1' },
			delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		} as const;
		messages.set([input]);
		activeToolsByThread.set(new Map([[THREAD_ID, [{
			id: 'tool-progress', name: 'test-tool', hint: 'in progress', status: 'running',
		}]]]));

		renderList();

		expect(host.querySelector('.pending-input-region')).not.toBeNull();
		expect(host.querySelector('.delivery-status')?.textContent).toContain('Sending when ready');

		applyLifecycle(THREAD_ID, { turnId: 'turn-1', turnOrder: 1 });
		handleMessage({
			type: 'message_added', thread_id: THREAD_ID,
			message: { ...input, active_turn_id: 'turn-1' },
		}, () => {});
		flushSync();

		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(host.querySelector('.delivery-status')).toBeNull();
		expect(host.querySelector('[data-delivery-action="cancel"]')).toBeNull();
		const row = host.querySelector<HTMLElement>('[data-message-id="active-input"]');
		const liveTool = host.querySelector<HTMLElement>('.tool-activity');
		expect(row).not.toBeNull();
		expect(liveTool).not.toBeNull();
		expect(row!.compareDocumentPosition(liveTool!) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBe(Node.DOCUMENT_POSITION_FOLLOWING);
	});

	it('settles a singleton through message_added without moving it above current-step output', () => {
		messages.set([
			{
				id: 'assistant-current-step', thread_id: THREAD_ID, role: 'assistant',
				content: 'current-step output stays above the accepted input',
				created_at: '2026-07-28T12:00:02Z', seq: 40,
			},
			{
				id: 'pending-input', thread_id: THREAD_ID, role: 'user',
				content: 'accept in place', created_at: '2026-07-28T12:00:00Z',
				metadata: { input_id: 'input-1' },
				delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
			},
		]);
		renderList();

		expect(rowIds()).toEqual(['assistant-current-step', 'pending-input']);

		handleMessage({
			type: 'message_added', thread_id: THREAD_ID, message: {
				id: 'pending-input', role: 'user', content: 'accept in place',
				created_at: '2026-07-28T12:00:00Z', metadata: { input_id: 'input-1' },
				delivery_state: 'delivered', delivery_error: null, delivery_revision: 2, seq: 41,
			},
		}, () => {});
		flushSync();

		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(rowIds()).toEqual(['assistant-current-step', 'pending-input']);
		expect(host.querySelectorAll('[data-message-id="pending-input"]')).toHaveLength(1);
		expect(host.querySelector('.delivery-status')).toBeNull();

		handleMessage({
			type: 'message_added', thread_id: THREAD_ID, message: {
				id: 'assistant-after-acceptance', role: 'assistant',
				content: 'this output may now flow below the accepted input',
				created_at: '2026-07-28T12:00:03Z', seq: 42,
			},
		}, () => {});
		flushSync();

		expect(rowIds()).toEqual([
			'assistant-current-step', 'pending-input', 'assistant-after-acceptance',
		]);
	});

	it('settles a messages_added batch atomically after current-step output in FIFO order', () => {
		messages.set([
			{
				id: 'pending-first', thread_id: THREAD_ID, role: 'user',
				content: 'first pending input', created_at: '2026-07-28T12:00:00Z',
				metadata: { input_id: 'input-1' }, delivery_state: 'queued', delivery_revision: 1,
			},
			{
				id: 'pending-second', thread_id: THREAD_ID, role: 'user',
				content: 'second pending input', created_at: '2026-07-28T12:00:01Z',
				metadata: { input_id: 'input-2' }, delivery_state: 'queued', delivery_revision: 1,
			},
			{
				id: 'assistant-current-step', thread_id: THREAD_ID, role: 'assistant',
				content: 'current-step output stays above both', created_at: '2026-07-28T12:00:02Z',
				seq: 40,
			},
		]);
		renderList();

		expect(rowIds(host.querySelector('.pending-input-region')!)).toEqual([
			'pending-first', 'pending-second',
		]);
		expect(rowIds()).toEqual([
			'assistant-current-step', 'pending-first', 'pending-second',
		]);

		const pendingSnapshots: string[][] = [];
		const unsubscribe = messages.subscribe((current) => {
			pendingSnapshots.push(current
				.filter((message) => message.delivery_state === 'queued')
				.map((message) => message.id));
		});
		pendingSnapshots.length = 0;

		handleMessage({
			type: 'messages_added', thread_id: THREAD_ID, messages: [
			{
				id: 'pending-first', role: 'user', content: 'first pending input',
				created_at: '2026-07-28T12:00:00Z', metadata: { input_id: 'input-1' },
				delivery_state: 'delivered', delivery_error: null, delivery_revision: 2, seq: 41,
			},
			{
				id: 'pending-second', role: 'user', content: 'second pending input',
				created_at: '2026-07-28T12:00:01Z', metadata: { input_id: 'input-2' },
				delivery_state: 'delivered', delivery_error: null, delivery_revision: 2, seq: 42,
			},
		],
		}, () => {});
		flushSync();
		unsubscribe();

		expect(pendingSnapshots).toEqual([[]]);
		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(rowIds()).toEqual([
			'assistant-current-step', 'pending-first', 'pending-second',
		]);
		expect(host.querySelectorAll('[data-message-id="pending-first"]')).toHaveLength(1);
		expect(host.querySelectorAll('[data-message-id="pending-second"]')).toHaveLength(1);
		expect(host.querySelector('.delivery-status')).toBeNull();
	});
});

function pendingInput(overrides: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		id: 'waiting-human', thread_id: THREAD_ID, role: 'user',
		content: 'waiting input', created_at: '2026-10-11T10:00:00Z',
		metadata: { input_id: 'input-1' }, delivery_state: 'queued', delivery_revision: 1,
		...overrides,
	};
}

function queuedInput(overrides: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		id: 'queued-input', thread_id: THREAD_ID, role: 'user',
		content: 'wait for this turn to finish', created_at: '2026-07-28T12:00:00Z',
		metadata: { input_id: 'input-q1', delivery_intent: 'queue_after_turn' },
		delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		...overrides,
	};
}

describe('MessageList: a queued input is not in the canvas at all', () => {
	// The canvas holds two things: what has been delivered, and what is about to
	// be. An input the user parked until the turn ends is neither — it is the
	// explicit instruction "not yet" — so it does not appear in the transcript
	// and it does not appear in the pending region either. It waits pinned to the
	// composer (`QueuedInputStack`, covered by InputBarQueuedInputs.test.ts).
	it('keeps a queued input out of the transcript and out of the pending region', () => {
		messages.set([
			queuedInput(),
			{
				id: 'assistant-text', thread_id: THREAD_ID, role: 'assistant',
				content: 'answer in progress', created_at: '2026-07-28T12:00:01Z', seq: 40,
			},
		]);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		activeToolsByThread.set(new Map([[THREAD_ID, [{
			id: 'tool-progress', name: 'test-tool', hint: 'in progress', status: 'running',
		}]]]));

		renderList();

		expect(rowIds()).toEqual(['assistant-text']);
		expect(host.querySelector('[data-message-id="queued-input"]')).toBeNull();
		// Nothing is imminent, so the region that means "imminent" is not there.
		expect(host.querySelector('.pending-input-region')).toBeNull();
		// The live indicator still renders; the queue simply is not near it.
		expect(host.querySelector('.tool-activity')).not.toBeNull();
	});

	it('never renders a queue as a sent bubble, or as any row the canvas owns', () => {
		// A steer interrupts the running turn; a queue deliberately waits it out.
		// Two opposite instructions must not render as the same green bubble —
		// and the queue does not get a canvas row of its own instead.
		token.set('test-token');
		messages.set([queuedInput()]);

		renderList();

		expect(host.querySelector('.user-bubble')).toBeNull();
		expect(host.querySelector('.queued-chip')).toBeNull();
		expect(rowIds()).toEqual([]);
		expect(host.querySelector('[data-delivery-action="cancel"]')).toBeNull();
	});

	it('excludes the optimistic echo of a queue too, before any receipt exists', () => {
		// The intent is the user's own instruction, so it is true the moment they
		// give it. A sent-looking bubble for one round trip is the same false claim
		// as leaving it in the canvas for one round trip.
		messages.set([queuedInput({
			_optimistic: true,
			delivery_state: undefined,
			delivery_error: undefined,
			delivery_revision: undefined,
		})]);

		renderList();

		expect(rowIds()).toEqual([]);
		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(host.querySelector('.user-bubble')).toBeNull();
	});

	it('excludes every queued input when several are waiting', () => {
		// Their FIFO order is shown where they live now, on the composer; the
		// canvas shows none of them. See InputBarQueuedInputs.test.ts.
		token.set('test-token');
		messages.set([
			queuedInput({ id: 'queued-first', content: 'first', metadata: { input_id: 'q1', delivery_intent: 'queue_after_turn' } }),
			queuedInput({ id: 'queued-second', content: 'second', created_at: '2026-07-28T12:00:01Z', metadata: { input_id: 'q2', delivery_intent: 'queue_after_turn' } }),
		]);

		renderList();

		expect(rowIds()).toEqual([]);
		expect(host.querySelector('.pending-input-region')).toBeNull();
	});

	it('keeps the imminent steer in the pending region and the queue out of it', () => {
		// Both are queued, but only one is being sent into the current step.
		// The region means imminent delivery and nothing else, so only the steer
		// is in it — the difference is a position on screen, not a caption.
		messages.set([
			{
				id: 'assistant-text', thread_id: THREAD_ID, role: 'assistant',
				content: 'answer in progress', created_at: '2026-07-28T12:00:00Z', seq: 40,
			},
			{
				id: 'steered-input', thread_id: THREAD_ID, role: 'user',
				content: 'apply this now', created_at: '2026-07-28T12:00:01Z',
				metadata: { input_id: 'input-1' },
				delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
			},
			queuedInput({ created_at: '2026-07-28T12:00:02Z' }),
		]);

		renderList();

		const region = host.querySelector<HTMLElement>('.pending-input-region');
		expect(region).not.toBeNull();
		expect(rowIds(region!)).toEqual(['steered-input']);
		expect(rowIds()).toEqual(['assistant-text', 'steered-input']);
		const steerRow = host.querySelector<HTMLElement>('[data-message-id="steered-input"]');
		expect(steerRow!.querySelector('.user-bubble')).not.toBeNull();
		expect(steerRow!.querySelector('.delivery-status')?.textContent)
			.toContain('Sending when ready');
		expect(host.querySelector('[data-message-id="queued-input"]')).toBeNull();
	});

	it('settles a queued message into real sequence once it is delivered', () => {
		messages.set([
			{
				id: 'assistant-current-step', thread_id: THREAD_ID, role: 'assistant',
				content: 'current-step output stays above the queued input',
				created_at: '2026-07-28T12:00:02Z', seq: 40,
			},
			queuedInput(),
		]);
		renderList();

		expect(rowIds()).toEqual(['assistant-current-step']);

		handleMessage({
			type: 'message_added', thread_id: THREAD_ID, message: {
				id: 'queued-input', role: 'user', content: 'wait for this turn to finish',
				created_at: '2026-07-28T12:00:00Z',
				metadata: { input_id: 'input-q1', delivery_intent: 'queue_after_turn' },
				delivery_state: 'delivered', delivery_error: null, delivery_revision: 2, seq: 41,
			},
		}, () => {});
		flushSync();

		// Delivered: it enters the transcript at the sequence position it earned,
		// and it now looks like every other message the user actually sent.
		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(host.querySelector('.user-bubble')).not.toBeNull();
		expect(rowIds()).toEqual(['assistant-current-step', 'queued-input']);
		expect(host.querySelectorAll('[data-message-id="queued-input"]')).toHaveLength(1);

		handleMessage({
			type: 'message_added', thread_id: THREAD_ID, message: {
				id: 'assistant-after-queue', role: 'assistant',
				content: 'the answer to the queued message', created_at: '2026-07-28T12:00:03Z',
				seq: 42,
			},
		}, () => {});
		flushSync();

		expect(rowIds()).toEqual([
			'assistant-current-step', 'queued-input', 'assistant-after-queue',
		]);
	});

	it('leaves a cancelled queue in the place its ending earned it', () => {
		// A terminal input takes a seq the moment it ends, so it renders in place
		// rather than hanging below the transcript forever.
		messages.set([
			queuedInput({
				delivery_state: 'cancelled', delivery_error: 'cancelled_by_user',
				delivery_revision: 2, seq: 40,
			}),
			{
				id: 'assistant-after', thread_id: THREAD_ID, role: 'assistant',
				content: 'output that followed the cancellation',
				created_at: '2026-07-28T12:00:05Z', seq: 41,
			},
		]);

		renderList();

		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(rowIds()).toEqual(['queued-input', 'assistant-after']);
		expect(host.querySelector('.delivery-status')?.textContent).toBe('Cancelled by you');
	});
});
