import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { OperatorNode, ThreadPlanStateEntry } from '$lib/api/plans';
import { THREAD, said, seedOpenTime, turn, work } from '$lib/__tests__/fixtures/focusRuns';
import { openRangeKey, openTimes } from '$lib/openTime';
import { waitRange } from './transcriptRunLabel';
import { coreSettings, type CoreSettings } from '$lib/stores/settings';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
import { activeToolsByThread } from '$lib/stores/threads';
import { formatMoment } from '$lib/time';
import { _resetTurnStateForTests, applyLifecycle, applyTerminal, requestInput } from '$lib/ws/turnState';
import { present } from '$lib/__tests__/fixtures/focusRuns';
import PlanChildControls from './plans/PlanChildControls.svelte';
import { nodeSubline } from './plans/planDisplay';
import { _resetLiveStepForTests } from './plans/planLiveStep';
import { setAsideLine, waitBack, waitChecksBack } from './plans/planWaits';
import { UNOPENABLE_DOCUMENT } from '$lib/openWaitArtifact';
import { waitMarkTitle } from './threadRowMark';
import TranscriptContent from './TranscriptContent.svelte';
import type { TranscriptEntry } from './messageListModel';

vi.mock('$lib/openWaitArtifact', async (original) => ({
	...(await original<typeof import('$lib/openWaitArtifact')>()),
	openWaitArtifact: vi.fn().mockResolvedValue(false),
}));

const NOW = new Date(2026, 9, 2, 10, 30, 0);
const at = (minutes: number): string => new Date(NOW.getTime() + minutes * 60_000).toISOString();

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

class ResizeObserverStub {
	observe() {}
	disconnect() {}
}

function waiting(until: string, label = 'CI on PR #1193'): ThreadPlanStateEntry {
	return { state: 'waiting', label_key: null, paused: false, attention_items: [], wait: { label, until, more: 0 } };
}

function focus(on: boolean): void {
	coreSettings.set({ features: { focus_mode: on } } as unknown as CoreSettings);
}

/** What the server answers for the wait: it has been open this long when the chat renders. */
const WAITED_SECONDS = 30 * 60;

function render(entries: TranscriptEntry[], props: Record<string, unknown> = {}): void {
	const turnSummaries = { t1: { ...turn('09:00:00', '10:00:00', 2), ended_at: at(-30) } };
	seedOpenTime(entries, turnSummaries, { now: Date.now() });
	const range = waitRange(entries, turnSummaries, true);
	if (range) {
		openTimes.update((readings) => new Map(readings).set(
			openRangeKey(THREAD, range),
			{ seconds: WAITED_SECONDS, openNow: true, receivedAt: Date.now(), floor: 0 },
		));
	}
	component = mount(TranscriptContent, {
		target: host,
		props: {
			entries, turnSummaries, renderedStreaming: false, historyComplete: true,
			expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
			onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
			threadId: THREAD, ...props,
		},
	});
	flushSync();
}

const trailingRun = () => present([work('a', 1, '09:30:00')]);
const afterReply = () => present([work('a', 1, '09:30:00'), said('reply', 2, '10:00:00')]);

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(NOW);
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetTurnStateForTests();
	_resetLiveStepForTests();
	activeToolsByThread.set(new Map());
	threadPlanStates.set({});
	vi.stubGlobal('ResizeObserver', ResizeObserverStub);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	coreSettings.set(null);
	threadPlanStates.set({});
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe('the waiting face in the chat', () => {
	it('shows the Focus chip as Waiting, with the label and time, in place of the settled face', () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: waiting(new Date(Date.now() + 40 * 60_000).toISOString()) });
		render(trailingRun());

		const chip = host.querySelector('.fold-toggle')!;
		expect(chip.textContent).toMatch(/Waiting\s*·\s*30 min\s*·\s*CI on PR #1193\s*·\s*back \d{1,2}:\d{2}\s?[AP]M/);
		expect(chip.textContent).not.toContain('Worked');
		expect(chip.querySelector('.waiting-mark')).not.toBeNull();
		expect(chip.querySelector('button.capsule-hit')!.getAttribute('aria-label')).toContain('Waiting for CI on PR #1193, 30 min so far. The agent checks back');
		expect(host.querySelector('.thinking-indicator, .live-spinner')).toBeNull();
	});

	it('stands alone after a reply, in the chip\'s place, never as a bare line', () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: waiting(new Date(Date.now() + 40 * 60_000).toISOString()) });
		render(afterReply());

		const chips = host.querySelectorAll('.fold-toggle');
		expect(chips).toHaveLength(2);
		expect(chips[1].textContent).toContain('Waiting');
		expect(host.querySelector('.wait-line')).toBeNull();
		// Nothing to open: no toggle, no chevron, no expand name.
		expect(chips[1].querySelector('button.capsule-hit, .fold-chevron, [aria-expanded]')).toBeNull();
		expect(chips[1].textContent).not.toMatch(/expand|collapse/i);
	});

	it('shows the chip once the last turn has ended: the turn state is terminal, not idle', () => {
		focus(true);
		applyLifecycle(THREAD, { turnId: 'turn-1', turnOrder: 1 });
		applyTerminal(THREAD, { turnId: 'turn-1', turnOrder: 1 });
		threadPlanStates.set({ [THREAD]: waiting(new Date(Date.now() + 40 * 60_000).toISOString()) });
		render(afterReply());

		expect(host.querySelectorAll('.fold-toggle')[1]?.textContent).toContain('Waiting');
	});

	it('shows no wait face while a turn runs', () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: waiting(new Date(Date.now() + 40 * 60_000).toISOString()) });
		render(trailingRun(), { renderedStreaming: true });
		expect(host.textContent).not.toContain('Waiting');
	});

	it('shows no wait face while a sent input waits to start a turn', () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: waiting(new Date(Date.now() + 40 * 60_000).toISOString()) });
		requestInput(THREAD, 'input-1');
		render(trailingRun());
		expect(host.textContent).not.toContain('Waiting');
		expect(host.querySelector('.fold-toggle')!.textContent).toContain('Worked');
	});

	it('shows the static face in the live-activity slot with Focus off, and a turn starting swaps it out in the same render', () => {
		focus(false);
		threadPlanStates.set({ [THREAD]: waiting(new Date(Date.now() + 40 * 60_000).toISOString()) });
		render(present([said('reply', 1, '10:00:00')]).filter((entry) => entry.kind === 'message'));

		const line = host.querySelector('.wait-line')!;
		expect(line.textContent).toMatch(/Waiting\s*·\s*30 min\s*·\s*CI on PR #1193\s*·\s*back/);
		expect(host.querySelector('.thinking-indicator, .dot')).toBeNull();

		requestInput(THREAD, 'input-2');
		flushSync();
		expect(host.querySelector('.wait-line')).toBeNull();
	});

	it('reads back now once the time has passed, never a past clock time', () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: waiting(new Date(Date.now() - 26 * 60_000).toISOString()) });
		render(trailingRun());

		const chip = host.querySelector('.fold-toggle')!;
		expect(chip.textContent).toMatch(/CI on PR #1193\s*·\s*back now/);
		expect(chip.textContent).not.toMatch(/stalled|overdue|due back/i);
		expect(chip.querySelector('button.capsule-hit')!.getAttribute('aria-label')).toContain('checks back now');
	});
});

describe('no surface shows a past time as back {time}', () => {
	const past = at(-26);

	function outsideNode(until: string): OperatorNode {
		return {
			title: 'Wait for CI', status_reason: 'Waiting for CI on PR #1193', child: null, review: null,
			last_transition_at: past, requires_user_action: false, artifacts: [],
			wait: { label: 'CI on PR #1193', until, more: 0 },
			display: { label_key: 'waiting', tone_key: 'waiting' },
			execution: {
				condition: 'waiting', primary_reason: null,
				causes: [{ kind: 'waiting', reason_key: 'deadline', reason: 'Waiting for CI on PR #1193', owner_kind: 'external', owner_id: null, watch_id: 'w1', ends_at: until }],
			},
		} as unknown as OperatorNode;
	}

	it('pane row: an outside wait reads the label and back now, then a future time once it has one', () => {
		expect(nodeSubline(outsideNode(past), NOW)).toBe('CI on PR #1193 · back now');
		expect(nodeSubline(outsideNode(at(40)), NOW)).toMatch(/^CI on PR #1193 · back \d{1,2}:\d{2} [AP]M$/);
	});

	it('set-aside row: names its time in the short form, then reads back now', () => {
		expect(setAsideLine(past, NOW)).toBe('Back now');
		expect(setAsideLine(at(40), NOW)).toMatch(/^Set aside until \d{1,2}:\d{2} [AP]M$/);
		expect(setAsideLine(at(40), NOW)).not.toMatch(/stalled|overdue|due back|wake/i);
	});

	it('sidebar title and the short and long forms agree', () => {
		expect(waitBack(past, NOW)).toBe('back now');
		expect(waitChecksBack(past, NOW)).toBe('checks back now');
		expect(waitMarkTitle(waiting(past), NOW)).toBe('Waiting for CI on PR #1193 · checks back now');
		expect(waitMarkTitle(waiting(at(40)), NOW)).toMatch(/checks back today at/);
	});

	it('child chip: a late child wait reads back now in its text, title and name', () => {
		const node = {
			id: 'n1', title: 'Check CI',
			child: { available: true, plan_id: 'p', thread_id: 't', progress: { done: 0, total: 1, open: 1, liveness: 'waiting' }, wait: { label: 'CI on PR #1193', until: past, more: 0 } },
		} as unknown as OperatorNode;
		component = mount(PlanChildControls, { target: host, props: { node, expanded: false, onToggle: () => {}, onOpen: () => {} } });
		flushSync();

		const chip = host.querySelector<HTMLElement>('.spawn-chip')!;
		expect(chip.textContent).toMatch(/CI on PR #1193\s*·\s*back now/);
		expect(chip.title).toBe('Waiting for CI on PR #1193, checks back now');
		expect(chip.getAttribute('aria-label')).toBe(chip.title);
		expect(chip.querySelector('.waiting-mark')).not.toBeNull();
		for (const text of [chip.textContent ?? '', chip.title]) expect(text).not.toMatch(/back \d|stalled|overdue|due back/i);
	});
});

describe('formatMoment compact', () => {
	const opts = { plain: true, clock: true, locales: ['en-US'] as Intl.LocalesArgument };
	const now = new Date(2026, 9, 21, 10, 0, 0);

	it('drops "today" and leaves the clock alone', () => {
		expect(formatMoment(new Date(2026, 9, 21, 23, 46), now, { ...opts, compact: true })).toBe('11:46 PM');
		expect(formatMoment(new Date(2026, 9, 21, 23, 46), now, opts)).toBe('today 11:46 PM');
	});

	it('leaves every other day as it is', () => {
		const compact = { ...opts, compact: true };
		expect(formatMoment(new Date(2026, 9, 22, 0, 45), now, compact)).toBe('tomorrow 12:45 AM');
		expect(formatMoment(new Date(2026, 9, 20, 9, 0), now, compact)).toBe('yesterday 9:00 AM');
		expect(formatMoment(new Date(2026, 9, 23, 9, 0), now, compact)).toBe('Friday 9:00 AM');
		expect(formatMoment(new Date(2026, 9, 30, 9, 0), now, compact)).toBe('Oct 30 9:00 AM');
	});

	it('still reads empty for an invalid date', () => {
		expect(formatMoment(new Date('nope'), now, { compact: true })).toBe('');
	});
});

describe('the label link is its own control', () => {
	const linked = (until: string): ThreadPlanStateEntry => ({
		...waiting(until), wait: { label: 'CI on PR #1193', until, more: 0, artifact_id: 'art-1' },
	});

	it('the chip link is a sibling of its toggle, reachable by its own name, and the toggle does not swallow it', () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: linked(at(40)) });
		render(trailingRun());

		const chip = host.querySelector('.fold-toggle')!;
		const link = chip.querySelector<HTMLButtonElement>('button.wait-link')!;
		expect(link.textContent).toBe('CI on PR #1193');
		expect(link.parentElement!.closest('button')).toBeNull();
		const toggle = chip.querySelector('button.capsule-hit')!;
		expect(toggle.contains(link)).toBe(false);
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		// Toggle first, then link, in focus order.
		expect(toggle.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		// The plain words are hidden from assistive technology; the link is not.
		expect(chip.querySelectorAll('[aria-hidden="true"]').length).toBeGreaterThan(0);
		expect(link.closest('[aria-hidden="true"]')).toBeNull();
	});

	it('the standalone chip keeps the link and has no toggle', () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: linked(at(40)) });
		render(afterReply());

		const chip = host.querySelectorAll('.fold-toggle')[1]!;
		expect(chip.querySelector('button.wait-link')).not.toBeNull();
		expect(chip.querySelectorAll('button')).toHaveLength(1);
	});

	it('a document that will not open says what to do next, once, from one shared string', async () => {
		focus(true);
		threadPlanStates.set({ [THREAD]: linked(at(40)) });
		render(afterReply());

		host.querySelector<HTMLButtonElement>('button.wait-link')!.click();
		await vi.waitFor(() => expect(host.querySelector('.wait-label[title]')?.getAttribute('title')).toBe(UNOPENABLE_DOCUMENT));
		expect(UNOPENABLE_DOCUMENT).toBe('Couldn\u2019t open this document. Ask the agent in chat for a working link.');
	});
});

describe('a wait time that cannot be formatted', () => {
	it('omits the time phrase instead of promising "soon"', () => {
		expect(waitBack('nope', NOW)).toBe('');
		expect(waitChecksBack('nope', NOW)).toBe('');
		expect(waitMarkTitle(waiting('nope'), NOW)).toBe('Waiting for CI on PR #1193');
	});
});
