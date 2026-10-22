import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { UpdateCard } from '$lib/api/threadUpdates';
import { _resetOpenTimeForTests } from '$lib/openTime';
import { token } from '$lib/stores/auth';
import { activeToolsByThread } from '$lib/stores/threads';
import { parseUTC } from '$lib/time';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';
import { DAY, THREAD, human, present, said, seedOpenTime, turn, work } from '$lib/__tests__/fixtures/focusRuns';
import { reactiveProps } from '$lib/__tests__/fixtures/reactiveProps.svelte';
import TranscriptContent from './TranscriptContent.svelte';
import { buildTranscriptPresentation, type TranscriptMessage } from './messageListModel';
import { attachPlanLimitPauses, planLimitPauseDescriptors } from './planLimitPause';
import {
	NO_LIVE_LINE, UNKNOWN_STEP, _resetLiveStepForTests, advanceLiveLine,
} from './plans/planLiveStep';
import type { TurnSummary as Summary } from './transcriptRunLabel';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(
	entries: ReturnType<typeof present>,
	turnSummaries: Record<string, Summary>,
	renderedStreaming = false,
): void {
	seedOpenTime(entries, turnSummaries);
	component = mount(TranscriptContent, {
		target: host,
		props: {
			entries, turnSummaries, renderedStreaming, historyComplete: true,
			expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
			threadId: THREAD,
		},
	});
	flushSync();
}

const toggles = () => [...host.querySelectorAll<HTMLButtonElement>('.fold-toggle')];
const faces = () => toggles().map((toggle) => toggle.textContent?.replace('▸', '').trim());

/** W1: one Turn, said-to-you update in the middle. */
function splitTurn() {
	return {
		rows: [
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:01:00'), work('b', 3, '10:02:00'),
			said('update', 4, '10:04:30'),
			work('c', 5, '10:06:00'),
			said('reply', 6, '10:10:00'),
		],
		summaries: { t1: turn('10:00:00', '10:10:00', 3) },
	};
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetTurnStateForTests();
	_resetLiveStepForTests();
	activeToolsByThread.set(new Map());
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeToolsByThread.set(new Map());
	token.set(null);
	vi.unstubAllGlobals();
});

describe('Focus runs: open time is asked for on its events only', () => {
	it('re-renders with the same runs ask nothing; a Turn ending asks again', async () => {
		const asked: string[][] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			if (String(input).includes('/api/open-time')) {
				asked.push((JSON.parse(String(init?.body)) as { items: { id: string }[] }).items.map((item) => item.id));
			}
			return { ok: true, status: 200, json: async () => ({ items: {} }) } as Response;
		}));
		_resetOpenTimeForTests();
		token.set('test-token');
		const settle = async () => { flushSync(); await Promise.resolve(); await Promise.resolve(); };
		const { rows, summaries } = splitTurn();
		const props = reactiveProps({
			entries: present(rows), turnSummaries: summaries as Record<string, Summary>,
			renderedStreaming: false, historyComplete: true,
			expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {}, threadId: THREAD,
		});
		component = mount(TranscriptContent, { target: host, props });
		await settle();
		expect(asked).toHaveLength(1);

		// What any tool or phase frame does upstream: the same runs, in a fresh array.
		props.entries = present(rows);
		await settle();
		props.entries = present(rows);
		await settle();
		expect(asked).toHaveLength(1);

		props.turnSummaries = { t1: turn('10:00:00', '10:12:00', 3) };
		await settle();
		expect(asked).toHaveLength(2);
	});
});

describe('Focus runs: each chip is its own run', () => {
	it('opens one run of a Turn without opening the Turn\'s other run', () => {
		const { rows, summaries } = splitTurn();
		render(present(rows), summaries);

		toggles()[0].click();
		flushSync();

		expect(toggles().map((toggle) => toggle.getAttribute('aria-expanded'))).toEqual(['true', 'false']);
	});

	it('gives each run of a split Turn its own steps and its own minutes (W1)', () => {
		const { rows, summaries } = splitTurn();
		render(present(rows), summaries);

		expect(faces()).toEqual(['Worked · 2 steps · 4 min', 'Worked · 1 step · 5 min']);
	});

	it('reads a naive start and an explicit-offset end as the same clock outside UTC (W11)', () => {
		const previousTz = process.env.TZ;
		process.env.TZ = 'America/Los_Angeles';
		try {
			const rows = [
				human('ask', 1, '04:00:00', 't1', '04:00:00'),
				work('a', 2, '04:01:00'),
				{ ...said('reply', 3, '04:03:00'), created_at: `${DAY}04:03:00+00:00` },
			];
			render(present(rows), { t1: { ...turn('04:00:00', null, 1), ended_at: `${DAY}04:03:00+00:00` } });

			expect(faces()).toEqual(['Worked · 1 step · 3 min']);
		} finally {
			if (previousTz === undefined) delete process.env.TZ;
			else process.env.TZ = previousTz;
		}
	});

	it('says one label in the chip text and to a screen reader, and names the body it controls', () => {
		const { rows, summaries } = splitTurn();
		render(present(rows), summaries);

		const [first] = toggles();
		expect(first.getAttribute('aria-label')).toBe('Expand Worked · 2 steps · 4 min');
		expect(first.getAttribute('aria-controls')).toBeNull();
		first.click();
		flushSync();

		expect(first.getAttribute('aria-label')).toBe('Collapse Worked · 2 steps · 4 min');
		const bodyId = first.getAttribute('aria-controls');
		expect(bodyId).toBeTruthy();
		const body = host.querySelector('.fold-details');
		expect(body?.id).toBe(bodyId);
		expect(host.querySelectorAll(`[id="${bodyId}"]`)).toHaveLength(1);
	});

	it('keeps a run open through Focus off and on, and through the run closing', () => {
		const rows = [human('ask', 1, '10:00:00', 't1', '10:00:00'), work('a', 2, '10:01:00')];
		const plain = (list: TranscriptMessage[]) => buildTranscriptPresentation(list, false);
		const props = reactiveProps({
			entries: present(rows, { live: { turnId: 't1' } }), turnSummaries: {} as Record<string, Summary>,
			renderedStreaming: false, historyComplete: true,
			expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {}, threadId: THREAD,
		});
		component = mount(TranscriptContent, { target: host, props });
		flushSync();
		toggles()[0].click();
		flushSync();
		expect(toggles()[0].getAttribute('aria-expanded')).toBe('true');

		props.entries = plain(rows);
		flushSync();
		expect(toggles()).toHaveLength(0);
		const closed = present([...rows, said('reply', 3, '10:02:00')]);
		const summaries = { t1: turn('10:00:00', '10:02:00', 1) };
		seedOpenTime(closed, summaries);
		props.entries = closed;
		props.turnSummaries = summaries;
		flushSync();

		expect(faces()).toEqual(['Worked · 1 step · 2 min']);
		expect(toggles()[0].getAttribute('aria-expanded')).toBe('true');
	});

	it('names a run that holds only context rows by its context, never "Worked"', () => {
		const context = {
			id: 'ctx', thread_id: THREAD, role: 'system', content: 'Plan refreshed', seq: 1,
			created_at: `${DAY}09:59:00`, activity_weight: 0,
			metadata: { type: 'context_event', summary: 'Plan refreshed' },
		};
		render(present([context, human('ask', 2, '10:00:00', null, null)]), {});

		expect(faces()).toEqual(['Context updated']);
	});
});

describe('Focus runs: the live run', () => {
	it('keeps earlier-Turn work and the running Turn in one live run', () => {
		const entries = present(
			[work('wake', 1, '10:00:00', 't1'), work('live', 2, '10:05:00', 't2')],
			{ live: { turnId: 't2' } },
		);

		expect(entries).toMatchObject([{ kind: 'fold', live: true, items: [
			{ message: { id: 'wake' } }, { message: { id: 'live' } },
		] }]);
	});

	it('speaks the live phrase without the ticking minutes', () => {
		activeToolsByThread.set(new Map([[THREAD, [
			{ id: 'tool', name: 'read_file', phrase: 'Reading source', status: 'running' },
		]]]) as never);
		applyLifecycle(THREAD, { turnId: 't1', turnOrder: 1 }, undefined, Date.now() - 65_000);
		render(present([work('a', 1, '10:00:00')], { live: { turnId: 't1' } }), {}, true);

		expect(toggles()[0].textContent).toContain('Working · Reading source');
		expect(toggles()[0].getAttribute('aria-label')).toBe('Expand current work: Reading source');
	});

	it('never recalls a phrase from before a shown update once the caller names the segment', () => {
		const concrete = { threadId: THREAD, phrase: 'Reading source', between: false, unknown: false, startedAt: 100 };
		const unknown = { threadId: THREAD, phrase: UNKNOWN_STEP, between: false, unknown: true, startedAt: 100 };
		const advance = advanceLiveLine as (...args: unknown[]) => ReturnType<typeof advanceLiveLine>;
		advance(NO_LIVE_LINE, concrete, false, 100, 'before-update');

		// The row after the shown update mounts fresh in the same Turn.
		const remounted = advance(NO_LIVE_LINE, unknown, false, 200, 'update');

		expect(remounted.show).toMatchObject({ phrase: UNKNOWN_STEP, unknown: true });
	});
});

describe('Focus runs: cards and paused Turns', () => {
	it('closes a run at a card recorded in the middle of it (W6)', () => {
		const rows = [10, 11, 12, 13, 14].map((seq) => work(`w${seq}`, seq, '10:00:00'));
		const card: UpdateCard = {
			id: 7, thread_id: THREAD, kind: 'failed', node_id: 'n1', title: 'Item',
			after_seq: 12, created_at: `${DAY}10:05:00`, node_exists: true, asks_user: true, blocked_asks_user: true,
		};

		const entries = present(rows, { cards: [card] });

		expect(entries.map((entry) => entry.kind)).toEqual(['fold', 'update', 'fold']);
		expect(entries.flatMap((entry) => (
			entry.kind === 'fold' ? [entry.items.map((item) => item.message.seq)] : []
		))).toEqual([[10, 11, 12], [13, 14]]);
	});

	it('marks a Turn the plan limit stopped before output on the run holding its hidden wake', () => {
		const wake = {
			...human('wake', 2, '10:00:00', 'paused', null),
			metadata: { sender_kind: 'agent' },
		};
		const rows = [human('ask', 1, '09:00:00', 'earlier', '09:00:00'), wake];
		const entries = present(rows);
		const pauses = planLimitPauseDescriptors(rows, { paused: { status: 'cancelled', cause: 'plan_limit_overage_off' } });
		const composed = attachPlanLimitPauses(entries, pauses);

		expect(composed.unavailable).toEqual([]);
		expect(composed.entries[1]).toMatchObject({ kind: 'fold', pauses: [{ pause: { source: wake }, afterItem: 0 }] });
		expect(pauses).toHaveLength(1);
	});
});

describe('server stamps', () => {
	it('keeps an explicit offset instead of reading the wall clock as UTC', () => {
		expect(parseUTC('2026-10-02T04:00:00+02:00').toISOString()).toBe('2026-10-02T02:00:00.000Z');
		expect(parseUTC('2026-10-02T04:00:00').toISOString()).toBe('2026-10-02T04:00:00.000Z');
		expect(parseUTC('2026-10-02T04:00:00Z').toISOString()).toBe('2026-10-02T04:00:00.000Z');
	});
});
