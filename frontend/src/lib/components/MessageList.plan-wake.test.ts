import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import MessageList from './MessageList.svelte';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { _resetCheckpointsForTests } from '$lib/stores/checkpoints';
import { coreSettings } from '$lib/stores/settings';

const THREAD = 'plan-wake-thread';
const FACTS = '{"next": {"executor_thread_id": "' + THREAD + '", "open_tasks": [{"id": "n1", "title": "E5"}]}}';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeAll(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(), cancel: vi.fn(), commitStyles: vi.fn(), onfinish: null, playState: 'finished',
		})) as unknown as Element['animate'];
	}
});

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD);
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	activeToolsByThread.set(new Map());
	coreSettings.set({ features: { focus_mode: false } } as any);
	messages.set([
		{
			// A wake as plan_wake writes it: the chip's own line in `summary`, the agent's text in `content`.
			id: 'plan-wake:child_returned:n1:3',
			thread_id: THREAD,
			role: 'system',
			content: '[CORSIA plan] The work delegated for E4 has returned and awaits your verdict.\n' + FACTS,
			created_at: '2026-09-13T00:00:00',
			metadata: {
				type: 'context_event', event_type: 'plan.child_returned', event_disposition: 'wake',
				summary: 'Delegated work returned for your review',
				origin: 'plan', plan_id: 'p1', node_id: 'n1',
			},
		},
		{
			id: 'plan-decision-answer:d1',
			thread_id: THREAD,
			role: 'system',
			content: '[PLAN DECISION ANSWERED]\nDecision d1: ship it.\n' + FACTS,
			created_at: '2026-09-13T00:00:01',
			metadata: {
				type: 'context_event', event_type: 'plan.decision_answered', event_disposition: 'wake',
				summary: 'Decision answered', origin: 'plan', plan_id: 'p1', decision_id: 'd1',
			},
		},
	]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	messages.set([]);
	coreSettings.set(null);
	_resetCheckpointsForTests();
	vi.restoreAllMocks();
});

describe('plan wakes in the transcript', () => {
	it('renders a plan input as an event chip with its one line of news, never as a user bubble or raw facts', () => {
		component = mount(MessageList, {
			target: host,
			props: {
				messagesEl: undefined,
				userIsNearBottom: true,
				ignoreScrollEvents: false,
				onOpenLightbox: () => {},
			},
		}) as Record<string, unknown>;
		flushSync();
		const chips = [...host.querySelectorAll('.ev-chip')];
		expect(chips).toHaveLength(2);
		expect(chips.map((c) => c.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
			'Plan Delegated work returned for your review',
			'Plan Decision answered',
		]);
		expect(host.textContent).not.toContain('open_tasks');
		expect(host.querySelector('.message.human-user')).toBeNull();
	});
});
