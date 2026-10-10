import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import TranscriptContent from './TranscriptContent.svelte';
import { planLimitPauseDescriptors } from './planLimitPause';
import focusTurnFixture from '../../../../testdata/focus-turn-summary-parity.json';
import { auto, compact, input, said, suppressed, work, summary, CAUSE } from '$lib/__tests__/fixtures/focusPauseProof';
import { buildMessageListPresentation, buildTranscriptPresentation } from './messageListModel';
import { mergeTranscriptUpdates } from './transcriptUpdates';
import { getTurnState, INITIAL_TURN_STATE, restoreTurnState } from '$lib/ws/turnState';
import { token } from '$lib/stores/auth';
import { get } from 'svelte/store';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('TranscriptContent update-card fold', () => {
	it('renders stable completed-turn labels and the legacy fallback', () => {
		// A run counts its own rows' server weights; this one is the whole Turn, so its weight is the Turn's count.
		const fold = (turnIds: string[], activity_weight?: number) => ({
			kind: 'fold' as const, id: `run:${turnIds.join('-')}`, turnIds, live: false,
			items: [{ kind: 'message' as const, id: `work-${turnIds.join('-')}`, showActivity: true, showContent: false,
				message: { id: `work-${turnIds.join('-')}`, role: 'assistant', content: '', activity_weight, metadata: { turn_id: turnIds[0] } } }],
		});
		// The next Turn's ask: it ends the run before it at that run's Turn end.
		const ask = (id: string, active_turn_id: string, write_started_at: string) => ({
			kind: 'message' as const, id: `message-${id}`, showActivity: true, showContent: true,
			message: { id, role: 'user', content: id, active_turn_id, write_started_at, metadata: {} },
		});
		component = mount(TranscriptContent, {
			target: host,
			props: {
				entries: [
					fold([focusTurnFixture.turn.id], focusTurnFixture.expected.activity_count),
					ask('ask-one', 'one', '2026-09-30T12:01:00Z'),
					fold(['one', 'two'], 1),
					ask('ask-legacy', 'legacy', '2026-09-30T12:09:00Z'),
					fold(['legacy']),
				],
				expandedTools: new Set(), expandedReasoning: new Set(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
				threadId: 'thread-1', renderedStreaming: false, historyComplete: true,
				turnSummaries: {
					one: { started_at: '2026-09-30T12:01:00Z', ended_at: '2026-09-30T12:04:00Z', status: 'completed', activity_count: 1 },
					[focusTurnFixture.turn.id]: {
						started_at: focusTurnFixture.turn.started_at,
						ended_at: focusTurnFixture.turn.ended_at,
						status: focusTurnFixture.turn.status,
						activity_count: focusTurnFixture.expected.activity_count,
					},
					two: { started_at: '2026-09-30T12:05:00Z', ended_at: '2026-09-30T12:08:00Z', status: 'completed', activity_count: 2 },
				},
			},
		});
		flushSync();

		const labels = [...host.querySelectorAll('.fold-toggle')].map((row) => row.textContent);
		expect(labels[0]).toContain(focusTurnFixture.expected.label);
		// A2: several turns read as one active time, never a "Background work" form.
		expect(labels[1]).toContain('Worked · 1 step · 6 min');
		expect(labels[2]).toContain('Worked');
	});

	it('never repeats whole-Turn totals across the blocks of one split Turn', () => {
		const block = (id: string, activity_weight: number) => ({
			kind: 'fold' as const, id, turnIds: ['turn-split'], live: false,
			items: [{
				kind: 'message' as const, id: `${id}-work`, showActivity: true, showContent: false,
				message: {
					id: `${id}-work`, role: 'assistant', content: '', activity_weight,
					metadata: { turn_id: 'turn-split' },
				},
			}],
		});
		component = mount(TranscriptContent, {
			target: host,
			props: {
				entries: [block('split-1', 2), block('split-2', 3), block('split-3', 108)],
				expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
				threadId: 'thread-1', renderedStreaming: false, historyComplete: true,
				turnSummaries: {
					'turn-split': {
						started_at: '2026-09-30T12:00:00Z', ended_at: '2026-09-30T12:37:00Z',
						status: 'completed', activity_count: 113,
					},
				},
			},
		});
		flushSync();

		const labels = [...host.querySelectorAll('.fold-toggle')].map((row) => row.textContent);
		// Each block shows only its own steps; none shows the whole Turn's 113.
		expect(labels[0]).toContain('Worked · 2 steps');
		expect(labels[1]).toContain('Worked · 3 steps');
		expect(labels[2]).toContain('Worked · 108 steps');
		for (const label of labels) expect(label).not.toContain('113');
	});

	it('discloses unseen messages above a truncated divider', () => {
		component = mount(TranscriptContent, {
			target: host,
			props: {
				entries: [{ kind: 'divider', id: 'visit-divider', truncated: true }],
				expandedTools: new Set(), expandedReasoning: new Set(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {},
				onOpenLightbox: () => {}, threadId: 'thread-1', renderedStreaming: false,
			},
		});
		flushSync();

		expect(host.querySelector('[data-visit-divider]')?.textContent).toContain('Since your last visit · more above');
	});

	it('labels only the turn the plan limit stopped, after its last entry, from the page summaries', () => {
		const message = (id: string, role: string, turnId: string) => ({
			kind: 'message' as const, id, showActivity: true, showContent: true,
			message: { id, role, content: id, seq: ({ 'ask-1': 1, 'work-1': 2, 'ask-2': 3, 'reply-2': 4 } as Record<string, number>)[id],
				delivery_state: role === 'user' ? 'delivered' : undefined, metadata: { turn_id: turnId } },
		});
		const summary = (cause?: string) => ({ started_at: null, ended_at: null, status: 'cancelled', activity_count: 1, cause });
		const entries = [
			message('ask-1', 'user', 'paused'),
			{ kind: 'fold' as const, id: 'fold-paused', turnIds: ['paused'], live: false, items: [message('work-1', 'assistant', 'paused')] },
			message('ask-2', 'user', 'stopped'), message('reply-2', 'assistant', 'stopped'),
		];
		const summaries = { paused: summary('plan_limit_overage_off'), stopped: summary() };
		const raw = entries.flatMap((entry) => entry.kind === 'fold' ? entry.items.map((item) => item.message) : [entry.message]);
		component = mount(TranscriptContent, {
			target: host,
			props: {
				entries, pauses: planLimitPauseDescriptors(raw, summaries),
				expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
				threadId: 'thread-1', renderedStreaming: false,
				turnSummaries: summaries,
			},
		});
		flushSync();

		const labels = host.querySelectorAll('.plan-limit-paused');
		expect([...labels].map((label) => label.textContent)).toEqual(['Paused: plan limit reached']);
		expect(labels[0].closest('.transcript-fold')).toBeTruthy();
		expect(labels[0].previousElementSibling?.textContent).toBe('Earlier work');
		host.querySelector<HTMLButtonElement>('.fold-toggle')!.click(); flushSync();
		expect(host.querySelector('.plan-limit-paused')?.previousElementSibling?.getAttribute('data-message-id')).toBe('work-1');
	});

	it('labels a turn the plan limit stopped before its first output, after the message it took', () => {
		const taken = (id: string, turnId: string | null) => ({
			kind: 'message' as const, id, showActivity: true, showContent: true,
			message: { id, role: 'user', content: id, seq: Number(id.at(-1)), delivery_state: 'delivered', metadata: {}, active_turn_id: turnId },
		});
		const entries = [taken('ask-1', 'paused'), taken('ask-2', 'stopped'), taken('ask-3', null)];
		component = mount(TranscriptContent, {
			target: host,
			props: {
				entries,
				pauses: planLimitPauseDescriptors(entries.map((entry) => entry.message), { paused: { status: 'cancelled', cause: 'plan_limit_overage_off' } }),
				expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
				threadId: 'thread-1', renderedStreaming: false,
				turnSummaries: {
					paused: { started_at: null, ended_at: null, status: 'cancelled', activity_count: 0, cause: 'plan_limit_overage_off' },
					stopped: { started_at: null, ended_at: null, status: 'cancelled', activity_count: 0 },
				},
			},
		});
		flushSync();

		const labels = [...host.querySelectorAll('.plan-limit-paused')];
		expect(labels.map((label) => label.textContent)).toEqual(['Paused: plan limit reached']);
		expect(labels[0].previousElementSibling?.textContent).toContain('ask-1');
		expect(labels[0].nextElementSibling?.textContent).toContain('ask-2');
	});
});

describe('Focus review boundary', () => {
	function renderBoundary(rows: ReturnType<typeof compact>[], withPause = false) {
		const summaries = withPause ? { p: summary('cancelled', 0, CAUSE) } : {};
		component = mount(TranscriptContent, { target: host, props: {
			entries: buildTranscriptPresentation(rows, true),
			pauses: withPause ? planLimitPauseDescriptors(rows, summaries) : [],
			expandedTools: new Set(), expandedReasoning: new Set(),
			onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
			threadId: 'focus-pause-proof', renderedStreaming: false, turnSummaries: summaries,
		} });
		flushSync();
	}
	for (const region of ['main non-wake', 'pending agent input'] as const)
		it(`B1 ${region} without a pause adds no collapsed delivery strip`, () => {
			const row = region === 'main non-wake'
				? { ...compact('non-wake', 1, 'p'), delivery_state: 'cancelled' }
				: { ...compact('agent-input', 1, 'p'), role: 'user', delivery_state: 'failed',
					metadata: { sender_kind: 'agent', sender_name: 'Worker' } };
			renderBoundary([row]);
			expect(host.querySelector('.fold-toggle')).toBeTruthy();
			expect(host.querySelectorAll('.ev-delivery, .delivery-status')).toHaveLength(0);
			expect(host.querySelector('.plan-limit-paused')).toBeNull();
		});
	it('B1 ordinary non-wake expansion and collapse stay free of added strips', () => {
		renderBoundary([{ ...compact('non-wake', 1, 'p'), delivery_state: 'cancelled' }]);
		host.querySelector<HTMLButtonElement>('.fold-toggle')!.click(); flushSync();
		expect(host.querySelectorAll('.ev-delivery, .delivery-status')).toHaveLength(0);
		host.querySelector<HTMLButtonElement>('.fold-toggle')!.click(); flushSync();
		expect(host.querySelectorAll('.ev-delivery, .delivery-status')).toHaveLength(0);
	});
	it('B1 stopped anchor keeps exactly one warning and existing retry control', () => {
		const previous = get(token);
		token.set('unit-test-auth');
		try {
			renderBoundary([{ ...auto('failed-anchor', 1, 'old', 'p'), delivery_state: 'failed',
				delivery_error: 'Refused', write_outcome: 'proved_no_write' }], true);
			expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(1);
			expect(host.querySelectorAll('.ev-delivery')).toHaveLength(1);
			expect(host.querySelectorAll('[data-delivery-action="retry"]')).toHaveLength(1);
			host.querySelector<HTMLButtonElement>('.fold-toggle')!.click(); flushSync();
			expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(1);
			expect(host.querySelectorAll('.ev-delivery')).toHaveLength(1);
			expect(host.querySelectorAll('[data-delivery-action="retry"]')).toHaveLength(1);
		} finally { token.set(previous); }
	});
	it('F1 renderer tracks the bound thread canonical active Turn', () => {
		const threadId = 'focus-pause-proof';
		const previous = getTurnState(threadId);
		const state = { ...INITIAL_TURN_STATE, phase: 'active' as const, turnId: 'n', turnOrder: 1 };
		try {
			restoreTurnState(threadId, state);
			const rows = [said('old1', 1, 'old'), suppressed('p2', 2, 'p'), work('n3', 3, 'n'),
				{ ...input('future', null, 'n'), delivery_state: 'queued' }];
			const summaries = { p: summary('cancelled', 0, CAUSE) };
			const pauses = planLimitPauseDescriptors(rows, summaries);
			const visible = buildMessageListPresentation(rows, pauses.map((pause) => pause.source)).visibleMessages;
			const entries = mergeTranscriptUpdates(buildTranscriptPresentation(visible, true), [], {
				focusMode: true, divider: { since: '2026-10-02T10:02:00Z', first_seq: 2 },
			});
			component = mount(TranscriptContent, { target: host, props: { entries, pauses, threadId,
				expandedTools: new Set(), expandedReasoning: new Set(), renderedStreaming: true,
				onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
			} });
			flushSync();
			expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(1);
			restoreTurnState(threadId, { ...state, turnId: 'other' }); flushSync();
			expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(0);
			restoreTurnState(threadId, state); flushSync();
			expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(1);
		} finally { restoreTurnState(threadId, previous); }
	});

	describe('artifact cards', () => {
		const askCard = { kind: 'ask', artifact_id: 'a1', title: 'Launch plan', type: 'document', icon: 'file-text', name: null,
			path: 'launch-plan.md', available: true, opens: true, hold_id: 'h1', state: 'needs_review' };
		// The reference to the same file under the Turn's closing text, covered by that ask.
		const refCard = { ...askCard, kind: 'artifact', hold_id: undefined, state: undefined, covered_by_ask: ['h1'] };
		const message = (id: string, cards: unknown[], content = 'Please review the plan.') => ({
			kind: 'message' as const, id, showActivity: false, showContent: true,
			message: { id, thread_id: 'thread-1', role: 'assistant', content, created_at: '2026-10-10T10:00:00', metadata: {}, cards } });
		const update = (blockedAsksUser: boolean) => ({ kind: 'update' as const, id: 'update-1', card: {
			id: 1, thread_id: 'thread-1', kind: 'blocked' as const, node_id: 'n1', title: 'Ship the export', after_seq: 1,
			created_at: '2026-10-10T10:00:01', node_exists: true, asks_user: true, blocked_asks_user: blockedAsksUser, ask: askCard } });
		const props = (entries: unknown[]) => ({ entries, expandedTools: new Set(), expandedReasoning: new Set(),
			onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
			threadId: 'thread-1', renderedStreaming: false, historyComplete: true });
		const render = (entries: unknown[]) => {
			component = mount(TranscriptContent, { target: host, props: props(entries) as never });
			flushSync();
		};
		const tileKinds = () => [...host.querySelectorAll<HTMLElement>('.tile')].map((tile) => tile.dataset.cardKind);

		it('homes the ask card on its blocked update row in every round state, so it does not move when answered', () => {
			render([message('m1', [askCard]), update(true)]);
			expect(tileKinds()).toEqual(['ask']);
			expect(host.querySelector('.update-ask .tile')).not.toBeNull();
			unmount(component!);

			render([message('m1', [askCard]), update(false)]);
			expect(tileKinds()).toEqual(['ask']);
			expect(host.querySelector('.update-ask .tile')).not.toBeNull();
		});

		it('with Focus off shows exactly one card for the reviewed document: the ask card', () => {
			render([message('step', [askCard], ''), update(true), message('final', [refCard])]);
			expect(tileKinds()).toEqual(['ask']);
			unmount(component!);

			render([message('step', [askCard], ''), message('final', [refCard])]);
			expect(tileKinds()).toEqual(['ask']);
		});

		it('with Focus on and the ask host collapsed shows exactly one card, the reference on the message', () => {
			const collapsed = { kind: 'fold' as const, id: 'run:turn-1', turnIds: ['turn-1'], live: false, items: [message('step', [askCard], '')] };
			render([collapsed, message('final', [refCard])]);
			expect(tileKinds()).toEqual(['artifact']);
			expect(host.querySelector('.tile')?.getAttribute('aria-label')).toContain('Launch plan');
		});
	});
});
