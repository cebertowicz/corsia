import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { clearThreadBuffers } from '$lib/stores/threads';
import { setWs, subscribedThreads } from '$lib/ws/state';
import {
	handleThreadSnapshot,
	handleToolDone,
	handleToolStart,
	handleTurnActivity,
	handleStepText,
	handleTurnComplete,
} from '$lib/ws/handlersTurns';
import { get } from 'svelte/store';
import { activeToolsByThread } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { FADE_MS, LINGER_MS, PHRASE_HOLD_MS, _resetLiveStepForTests } from '$lib/components/plans/planLiveStep';
import { _resetOpenTimeForTests } from '$lib/openTime';
import { operatorNode } from './fixtures/operatorPlan';
import { withLiveFolds } from './fixtures/planFolds';
import { childState } from './fixtures/planFolds';

/**
 * PRD-ATTENTION-SIGNAL AS14: a plan item whose worker runs, or that its own
 * running Turn works, shows the current step and elapsed time as its subline,
 * and nothing once the work stops. Frames go through the real handlers, so
 * the live path and the reload path meet in the same stores.
 */

// The server's answer for a running step is the time since its first try; answered in the same tick so these
// synchronous tests read it the way a reload with the answer already in hand does.
vi.mock('$lib/openTime', async (importActual) => {
	const actual = await importActual<typeof import('$lib/openTime')>();
	return {
		...actual,
		requestOpenTime: (queries: readonly { key: string; since: number }[]) => {
			const now = Date.now();
			actual.openTimes.update((readings) => {
				const next = new Map(readings);
				for (const query of queries) next.set(query.key, { seconds: (now - query.since) / 1000, openNow: true, receivedAt: now });
				return next;
			});
		},
	};
});

const NOW = Date.parse('2026-09-26T18:00:00Z');
const THREE_MIN_AGO = '2026-09-26T17:57:00+00:00';
const READING = 'Reading PRD-ATTENTION-SIGNAL.md';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let sent: Array<{ type: string; thread_id: string }>;

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(NOW);
	// The shared per-Turn phrase memory outlives a mount; each test starts clean.
	_resetLiveStepForTests();
	_resetOpenTimeForTests();
	sent = [];
	setWs({ readyState: 1, send: (raw: string) => sent.push(JSON.parse(raw)) } as unknown as WebSocket);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	for (const threadId of ['worker', 'owner', 'peer']) clearThreadBuffers(threadId);
	subscribedThreads.clear();
	setWs(null);
	_resetTurnStateForTests();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

/** The step's first try, three minutes before the tests' clock: what the server counts its open time from. */
const firstTry = { id: 'try-1', ordinal: 1, executor: { thread_id: 'worker', label: 'Worker' }, requirements_revision: 1, selected_at: THREE_MIN_AGO, released_at: null, release_reason: null };

function delegated(): OperatorNode {
	const base = operatorNode({ id: 'delegated', title: 'Delegated step' });
	return {
		...base,
		display: { label_key: 'waiting', tone_key: 'waiting' },
		execution: { ...base.execution, condition: 'waiting', attempts: [firstTry] },
		child: {
			available: true, plan_id: 'child-plan', thread_id: 'worker',
			progress: { done: 0, total: 1, open: 1, liveness: 'advancing' },
		},
	};
}

function ownTask(): OperatorNode {
	const base = operatorNode({ id: 'own', title: 'Own step', state: 'active' });
	return {
		...base,
		execution: {
			...base.execution,
			attempts: [firstTry],
			selection: {
				attempt_id: 'a1', attempt_ordinal: 1, executor_thread_id: 'owner',
				requirements_revision: 1, turn_active: true, in_flight_dispatch_count: 0,
			},
		},
	};
}

function render(nodes: OperatorNode[], extra: Record<string, unknown> = {}): { nodes: OperatorNode[] } {
	const props = $state({ nodes, ...extra });
	component = mount(PlanTree, { target: host, props: withLiveFolds(props) });
	flushSync();
	return props;
}

function terminalCopy(node: OperatorNode): OperatorNode {
	return { ...node, execution: { ...node.execution, condition: 'terminal' } };
}

function delegatedTo(threadId: string, title: string): OperatorNode {
	const base = delegated();
	return { ...base, id: `delegated-${threadId}`, title, child: { ...base.child!, thread_id: threadId } };
}

function boundChild(): OperatorNode {
	const base = ownTask();
	return {
		...base, id: 'bound', title: 'Bound step',
		execution: {
			...base.execution,
			selection: { ...base.execution.selection!, executor_thread_id: 'worker' },
		},
	};
}

function startWorker(): void {
	handleTurnActivity({ type: 'turn_activity', ...turn('worker'), started_at: THREE_MIN_AGO });
	handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't1', name: 'read_file', phrase: READING });
	flushSync();
}

function liveStepElement(title: string): HTMLElement | null {
	const row = [...host.querySelectorAll('.tree-row')]
		.find((item) => item.querySelector('.row-title')?.textContent === title);
	return row?.querySelector<HTMLElement>('.live-step') ?? null;
}

function spinning(title: string): boolean {
	const row = [...host.querySelectorAll('.tree-row')]
		.find((item) => item.querySelector('.row-title')?.textContent === title);
	if (!row) throw new Error(`no row titled ${title}`);
	return row.querySelector('.state-icon')!.classList.contains('spinning');
}

function reducedMotion(matches: boolean): void {
	vi.stubGlobal('matchMedia', (query: string) => ({ matches, media: query }));
}

function subline(title: string): string | null {
	const row = [...host.querySelectorAll('.tree-row')]
		.find((item) => item.querySelector('.row-title')?.textContent === title);
	if (!row) throw new Error(`no row titled ${title}`);
	return row.querySelector('.row-subline')?.textContent?.trim() ?? null;
}

const turn = (threadId: string) => ({ thread_id: threadId, turn_id: `turn-${threadId}`, turn_order: 4 });

describe('plan item live step (AS14)', () => {
	it('shows a running worker\'s current step and elapsed time, and follows it', () => {
		render([delegated()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('worker'), started_at: THREE_MIN_AGO });
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't1', name: 'read_file', phrase: READING });
		flushSync();

		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
		// The row follows the worker's Turn frames though no transcript shows it.
		expect(sent).toContainEqual({ type: 'subscribe_session', thread_id: 'worker' });

		vi.advanceTimersByTime(2 * 60_000);
		flushSync();
		expect(subline('Delegated step')).toBe(`Working 5 min · ${READING}`);

		// Between tools the last phrase stays, dimmed; it does not flip to "Working".
		handleToolDone({ type: 'tool_done', ...turn('worker'), tool_id: 't1', phrase: READING });
		flushSync();
		expect(subline('Delegated step')).toBe(`Working 5 min · ${READING}`);
		expect(liveStepElement('Delegated step')?.classList.contains('dimmed')).toBe(true);
	});

	it('reads Working, undimmed, before the Turn\'s first tool', () => {
		render([delegated()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('worker'), started_at: THREE_MIN_AGO });
		flushSync();

		expect(subline('Delegated step')).toBe('Working 3 min');
		expect(liveStepElement('Delegated step')?.classList.contains('dimmed')).toBe(false);
	});

	it('holds a phrase for its minimum time, then crossfades to the newest', () => {
		render([delegated()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('worker'), started_at: THREE_MIN_AGO });
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't1', name: 'read_file', phrase: READING });
		flushSync();

		// A burst of fast tools: none shows until the hold ends, and the last wins.
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't2', name: 'grep', phrase: 'Searching plans' });
		flushSync();
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't3', name: 'bash', phrase: 'Running tests' });
		flushSync();
		vi.advanceTimersByTime(PHRASE_HOLD_MS - 100);
		flushSync();
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);

		// The hold ends: the line fades out, then the newest phrase fades in.
		vi.advanceTimersByTime(100);
		flushSync();
		expect(liveStepElement('Delegated step')?.classList.contains('fading')).toBe(true);
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
		vi.advanceTimersByTime(FADE_MS);
		flushSync();
		expect(subline('Delegated step')).toBe('Working 3 min · Running tests');
		expect(liveStepElement('Delegated step')?.classList.contains('fading')).toBe(false);
	});

	it('lands a changed phrase after hold and fade even when unrelated frames keep arriving', () => {
		render([delegated()]);
		startWorker();
		const opacityStates: boolean[] = [];
		const watch = () => opacityStates.push(liveStepElement('Delegated step')?.classList.contains('fading') ?? false);

		// Tool frames faster than the fade, none changing the phrase: the line stays full and unbroken.
		for (let tick = 0; tick < 12; tick += 1) {
			handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: `same-${tick}`, name: 'read_file', phrase: READING });
			flushSync();
			vi.advanceTimersByTime(FADE_MS / 4);
			flushSync();
			watch();
		}
		expect(opacityStates.every((fading) => !fading)).toBe(true);

		// The phrase changes, then frames keep arriving faster than the fade all through hold and fade.
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't2', name: 'grep', phrase: 'Searching plans' });
		flushSync();
		const fadingStates: boolean[] = [];
		// Frames never pause for a full fade, so the phrase lands only if they cannot restart it.
		for (let elapsed = 0; elapsed < PHRASE_HOLD_MS + 2 * FADE_MS; elapsed += FADE_MS / 4) {
			handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: `next-${elapsed}`, name: 'grep', phrase: 'Searching plans' });
			flushSync();
			vi.advanceTimersByTime(FADE_MS / 4);
			flushSync();
			fadingStates.push(liveStepElement('Delegated step')?.classList.contains('fading') ?? false);
		}
		expect(subline('Delegated step')).toBe('Working 3 min · Searching plans');
		// One fade: off, on for a single contiguous run, then off once the phrase lands.
		const firstFade = fadingStates.indexOf(true);
		expect(firstFade).toBeGreaterThan(-1);
		const lastFade = fadingStates.lastIndexOf(true);
		expect(fadingStates.slice(firstFade, lastFade + 1).every(Boolean)).toBe(true);
	});

	it('drives the crossfade duration from FADE_MS', () => {
		render([delegated()]);
		startWorker();
		expect(liveStepElement('Delegated step')?.style.getPropertyValue('--fade-ms')).toBe(`${FADE_MS}ms`);
	});

	it('does not carry a phrase into a successor Turn when neither Turn names its start', () => {
		render([delegated()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('worker') });
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't1', name: 'read_file', phrase: READING });
		flushSync();
		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		flushSync();
		handleTurnActivity({ type: 'turn_activity', thread_id: 'worker', turn_id: 'turn-2', turn_order: 5 });
		flushSync();
		vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
		flushSync();
		expect(subline('Delegated step')).toBe('Working 3 min');
	});

	it('respects reduced motion: no fade, the minimum hold stays', () => {
		reducedMotion(true);
		render([delegated()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('worker'), started_at: THREE_MIN_AGO });
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't1', name: 'read_file', phrase: READING });
		flushSync();
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't2', name: 'grep', phrase: 'Searching plans' });
		flushSync();

		vi.advanceTimersByTime(PHRASE_HOLD_MS - 100);
		flushSync();
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
		vi.advanceTimersByTime(100);
		flushSync();
		expect(subline('Delegated step')).toBe('Working 3 min · Searching plans');
		expect(liveStepElement('Delegated step')?.classList.contains('fading')).toBe(false);
	});

	it('keeps the line through the gap between one Turn and its successor', () => {
		render([delegated()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('worker'), started_at: THREE_MIN_AGO });
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't1', name: 'read_file', phrase: READING });
		flushSync();

		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		flushSync();
		expect(subline('Delegated step')).toContain(READING);
		vi.advanceTimersByTime(LINGER_MS + 10);
		flushSync();
		expect(subline('Delegated step')).toBeNull();
	});

	it('clears a lingering step when the node goes terminal inside the linger window', () => {
		const props = render([delegated()]);
		startWorker();
		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		flushSync();
		expect(subline('Delegated step')).toContain(READING);

		vi.advanceTimersByTime(LINGER_MS / 2);
		props.nodes = [terminalCopy(delegated())];
		flushSync();
		expect(subline('Delegated step')).toBeNull();
		vi.advanceTimersByTime(LINGER_MS);
		flushSync();
		expect(subline('Delegated step')).toBeNull();
	});

	it('does not extend the linger when the node refreshes during it', () => {
		const props = render([delegated()]);
		startWorker();
		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		flushSync();

		vi.advanceTimersByTime(LINGER_MS - 500);
		props.nodes = [delegated()];
		flushSync();
		expect(subline('Delegated step')).toContain(READING);
		vi.advanceTimersByTime(600);
		flushSync();
		expect(subline('Delegated step')).toBeNull();
	});

	it('continues into a successor Turn inside the linger window without a remount', () => {
		render([delegated()]);
		startWorker();
		const before = liveStepElement('Delegated step');
		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		flushSync();
		vi.advanceTimersByTime(LINGER_MS / 2);
		handleTurnActivity({ type: 'turn_activity', thread_id: 'worker', turn_id: 'turn-2', turn_order: 5, started_at: THREE_MIN_AGO });
		flushSync();

		expect(liveStepElement('Delegated step')).toBe(before);
		vi.advanceTimersByTime(LINGER_MS * 2);
		flushSync();
		expect(liveStepElement('Delegated step')).toBe(before);
	});

	it('drops a parent\'s line only when a rendered child row shows the same thread', () => {
		const ready = (nodes: OperatorNode[]) => (childState({ status: 'ready', planId: 'child-plan', nodes, currentNodeIds: [], nextNodeIds: [], errorMessage: null }));
		const state = $state({ nodes: [ownTask()] });
		const expanded = { isChildExpanded: (id: string) => id === 'delegated', childRowState: () => ready(state.nodes) };
		render([delegated()], expanded);
		startWorker();
		// The expanded child has no row bound to the worker: the parent keeps its line.
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);

		state.nodes = [boundChild()];
		flushSync();
		expect(subline('Delegated step')).toBeNull();
		expect(subline('Bound step')).toBe(`Working 3 min · ${READING}`);
	});

	it('spins a branch while its own step runs, and moves the line to the lowest visible row', () => {
		const working = { label_key: 'working', tone_key: 'active' } as const;
		const phase = operatorNode({ id: 'phase', title: 'Phase', display: working });
		render([phase, { ...ownTask(), parent_id: 'phase', depth: 1 }], { expandBranchesByDefault: true });
		handleTurnActivity({ type: 'turn_activity', ...turn('owner'), started_at: THREE_MIN_AGO });
		handleToolStart({ type: 'tool_start', ...turn('owner'), tool_id: 't1', name: 'read_file', phrase: READING });
		flushSync();
		// Open: the step carries the line, and the branch still spins because its work moves.
		expect(subline('Own step')).toBe(`Working 3 min · ${READING}`);
		expect(subline('Phase')).toBeNull();
		expect(spinning('Own step')).toBe(true);
		expect(spinning('Phase')).toBe(true);

		// Collapsed: the branch is the lowest visible row, so it carries the line too.
		host.querySelector<HTMLButtonElement>('.branch-toggle')!.click();
		flushSync();
		expect(host.textContent).not.toContain('Own step');
		expect(subline('Phase')).toBe(`Working 3 min · ${READING}`);
		expect(spinning('Phase')).toBe(true);
	});

	it('keeps a delegated step spinning after its subagent\'s row takes the line', () => {
		const step = { ...delegated(), display: { label_key: 'working', tone_key: 'active' } } as OperatorNode;
		const ready = childState({ status: 'ready', planId: 'child-plan', nodes: [boundChild()], currentNodeIds: [], nextNodeIds: [], errorMessage: null });
		const props = $state({ open: false });
		render([step], { isChildExpanded: () => props.open, childRowState: () => ready });
		startWorker();
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
		expect(spinning('Delegated step')).toBe(true);

		props.open = true;
		flushSync();
		expect(subline('Bound step')).toBe(`Working 3 min · ${READING}`);
		expect(subline('Delegated step')).toBeNull();
		expect(spinning('Delegated step')).toBe(true);
	});

	it('puts the line on a collapsed branch holding the row bound to the worker, and hands it down when it opens', () => {
		const group = operatorNode({ id: 'grp', title: 'Group' });
		const nested = { ...boundChild(), parent_id: 'grp', depth: 1 };
		const other = operatorNode({ id: 'other', title: 'Other step' });
		const ready = childState({ status: 'ready', planId: 'child-plan', nodes: [group, nested, other], currentNodeIds: [], nextNodeIds: [], errorMessage: null });
		render([delegated()], { isChildExpanded: (id: string) => id === 'delegated', childRowState: () => ready });
		startWorker();
		expect(host.textContent).not.toContain('Bound step');
		// The collapsed branch is the lowest visible row over the worker's step: one line, on it.
		expect(subline('Group')).toBe(`Working 3 min · ${READING}`);
		expect(subline('Delegated step')).toBeNull();

		// Opening the branch renders the bound row, and only then does the branch hand the line down.
		host.querySelector<HTMLButtonElement>('.branch-toggle')!.click();
		flushSync();
		expect(subline('Bound step')).toBe(`Working 3 min · ${READING}`);
		expect(subline('Group')).toBeNull();
		expect(subline('Delegated step')).toBeNull();
	});

	// #3354: the server reads a delegated step Working while work moves at any depth under its
	// worker; the pane knows no thread for that deeper work, so the spin follows the server.
	it('spins a delegated step, and the branch over it, while only a deeper worker runs', () => {
		const working = { label_key: 'working', tone_key: 'active' } as const;
		const phase = operatorNode({ id: 'phase', title: 'Phase', display: working });
		const step = { ...delegated(), display: working, parent_id: 'phase', depth: 1 } as OperatorNode;
		render([phase, step], { expandBranchesByDefault: true });

		// No Turn of the step's own worker runs: it handed the work on and waits.
		expect(spinning('Delegated step')).toBe(true);
		expect(spinning('Phase')).toBe(true);
	});

	it('does not spin a step that reads Working only because it awaits its agent\'s review', () => {
		const working = { label_key: 'working', tone_key: 'active' } as const;
		const phase = operatorNode({ id: 'phase', title: 'Phase', display: working });
		const base = operatorNode({ id: 'reviewed', title: 'Reviewed step', display: working, parent_id: 'phase', depth: 1 });
		const reviewed = { ...base, execution: { ...base.execution, condition: 'review' } } as OperatorNode;
		render([phase, reviewed], { expandBranchesByDefault: true });

		expect(spinning('Reviewed step')).toBe(false);
		expect(spinning('Phase')).toBe(false);
	});

	it('keeps a delegated step\'s line when the row bound to its worker is folded away', () => {
		const hidden = { ...boundChild(), folded: true };
		const other = operatorNode({ id: 'other', title: 'Other step' });
		const ready = childState({ status: 'ready', planId: 'child-plan', nodes: [hidden, other], currentNodeIds: [], nextNodeIds: [], errorMessage: null });
		render([delegated()], { isChildExpanded: (id: string) => id === 'delegated', childRowState: () => ready });
		startWorker();

		// No rendered row shows the worker's step, so the delegated step still owes the line.
		expect(host.textContent).not.toContain('Bound step');
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
	});

	// #3039: the row a branch expansion mounts must inherit the Turn's phrase, not read "Working".
	// A row's own live-line memory is per mounted component, so the seam is the handover itself.
	for (const motion of [false, true]) {
		it(`hands the same Turn's dimmed phrase to a branch that expands after the tool store empties (${motion ? 'reduced motion' : 'normal motion'})`, () => {
			if (motion) reducedMotion(true);
			const group = operatorNode({ id: 'grp', title: 'Group' });
			const nested = { ...boundChild(), parent_id: 'grp', depth: 1 };
			const other = operatorNode({ id: 'other', title: 'Other step' });
			const ready = childState({ status: 'ready', planId: 'child-plan', nodes: [group, nested, other], currentNodeIds: [], nextNodeIds: [], errorMessage: null });
			render([delegated()], { isChildExpanded: (id: string) => id === 'delegated', childRowState: () => ready });
			startWorker();
			// The Turn's only tool finalizes: the store empties mid-Turn, so a later row sees no phrase.
			handleToolDone({ type: 'tool_done', ...turn('worker'), tool_id: 't1', phrase: READING });
			handleStepText({
				type: 'step_text', ...turn('worker'), message_id: 'm1', created_at: '2026-09-26T17:59:00Z',
				content: 'checked', finalized_tool_ids: ['t1'],
			});
			flushSync();
			expect(get(activeToolsByThread).get('worker') ?? []).toEqual([]);
			vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
			flushSync();
			expect(subline('Group')).toBe(`Working 3 min · ${READING}`);

			host.querySelector<HTMLButtonElement>('.branch-toggle')!.click();
			flushSync();
			expect(subline('Bound step')).toBe(`Working 3 min · ${READING}`);
			expect(liveStepElement('Bound step')?.classList.contains('dimmed')).toBe(true);
			expect(subline('Group')).toBeNull();

			// Collapsing hands the line back to the branch, still dimmed and still the same phrase.
			host.querySelector<HTMLButtonElement>('.branch-toggle')!.click();
			flushSync();
			expect(host.textContent).not.toContain('Bound step');
			expect(subline('Group')).toBe(`Working 3 min · ${READING}`);
			expect(liveStepElement('Group')?.classList.contains('dimmed')).toBe(true);
			expect(subline('Delegated step')).toBeNull();
		});
	}

	it('does not hand one thread\'s phrase to a Turn that has shown no tool', () => {
		render([delegated(), delegatedTo('peer', 'Peer step')]);
		startWorker();
		handleToolDone({ type: 'tool_done', ...turn('worker'), tool_id: 't1', phrase: READING });
		handleStepText({
			type: 'step_text', ...turn('worker'), message_id: 'm1', created_at: '2026-09-26T17:59:00Z',
			content: 'checked', finalized_tool_ids: ['t1'],
		});
		handleTurnActivity({ type: 'turn_activity', ...turn('peer'), started_at: THREE_MIN_AGO });
		flushSync();
		vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
		flushSync();

		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
		expect(subline('Peer step')).toBe('Working 3 min');
	});

	it('does not hand the previous Turn\'s phrase to a fresh row for the same thread', () => {
		const group = operatorNode({ id: 'grp', title: 'Group' });
		const nested = { ...boundChild(), parent_id: 'grp', depth: 1 };
		const other = operatorNode({ id: 'other', title: 'Other step' });
		const ready = childState({ status: 'ready', planId: 'child-plan', nodes: [group, nested, other], currentNodeIds: [], nextNodeIds: [], errorMessage: null });
		render([delegated()], { isChildExpanded: (id: string) => id === 'delegated', childRowState: () => ready });
		startWorker();
		handleStepText({
			type: 'step_text', ...turn('worker'), message_id: 'm1', created_at: '2026-09-26T17:59:00Z',
			content: 'checked', finalized_tool_ids: ['t1'],
		});
		flushSync();
		vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
		flushSync();
		expect(subline('Group')).toBe(`Working 3 min · ${READING}`);

		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'worker', turn_id: 'turn-2', turn_order: 5,
			started_at: '2026-09-26T17:59:59+00:00',
		});
		flushSync();
		vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
		flushSync();
		expect(subline('Group')).toBe('Working 3 min');

		host.querySelector<HTMLButtonElement>('.branch-toggle')!.click();
		flushSync();
		expect(subline('Bound step')).toBe('Working 3 min');
		expect(liveStepElement('Bound step')?.classList.contains('dimmed')).toBe(false);
		expect(subline('Group')).toBeNull();
		expect(subline('Delegated step')).toBeNull();
	});

	it('keeps the last phrase, dimmed, when a step_text frame empties the tool store mid-Turn', () => {
		render([delegated()]);
		startWorker();
		handleToolDone({ type: 'tool_done', ...turn('worker'), tool_id: 't1', phrase: READING });
		handleStepText({
			type: 'step_text', ...turn('worker'), message_id: 'm1', created_at: '2026-09-26T17:59:00Z',
			content: 'checked', finalized_tool_ids: ['t1'],
		});
		flushSync();
		expect(get(activeToolsByThread).get('worker') ?? []).toEqual([]);

		vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
		flushSync();
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
		expect(liveStepElement('Delegated step')?.classList.contains('dimmed')).toBe(true);

		// The next tool replaces it; the Turn's first tool never went through "Working".
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't2', name: 'grep', phrase: 'Searching plans' });
		flushSync();
		vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
		flushSync();
		expect(subline('Delegated step')).toBe('Working 3 min · Searching plans');
	});

	it('reads Working for a successor Turn that has shown no tool, not the previous Turn\'s phrase', () => {
		render([delegated()]);
		startWorker();
		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		flushSync();
		handleTurnActivity({ type: 'turn_activity', thread_id: 'worker', turn_id: 'turn-2', turn_order: 5, started_at: '2026-09-26T17:59:59+00:00' });
		flushSync();
		vi.advanceTimersByTime(PHRASE_HOLD_MS + FADE_MS + 10);
		flushSync();
		expect(subline('Delegated step')).toBe('Working 3 min');
	});

	it('keeps a parent\'s line when its subagent\'s plan has not loaded, or is collapsed', () => {
		const loading = childState({ status: 'loading', planId: null, nodes: [], currentNodeIds: [], nextNodeIds: [], errorMessage: null });
		render([delegated()], { isChildExpanded: () => true, childRowState: () => loading });
		startWorker();
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
	});

	it('shows the step of the agent\'s own Turn bound to the item', () => {
		render([ownTask()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('owner'), started_at: '2026-09-26T17:59:40+00:00' });
		flushSync();

		expect(subline('Own step')).toBe('Working 3 min');
	});

	it('shows nothing when no work runs for the item', () => {
		render([delegated(), ownTask()]);

		expect(subline('Delegated step')).toBeNull();
		expect(subline('Own step')).toBeNull();
		expect(sent).toEqual([]);
	});

	it('drops the subline and stops following when the work stops', () => {
		render([delegated()]);
		handleTurnActivity({ type: 'turn_activity', ...turn('worker'), started_at: THREE_MIN_AGO });
		handleToolStart({ type: 'tool_start', ...turn('worker'), tool_id: 't1', name: 'read_file', phrase: READING });
		flushSync();
		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);

		handleTurnComplete({ type: 'turn_complete', ...turn('worker') });
		flushSync();
		vi.advanceTimersByTime(LINGER_MS + 10);
		flushSync();

		expect(subline('Delegated step')).toBeNull();
		expect(sent).toContainEqual({ type: 'unsubscribe_session', thread_id: 'worker' });
	});

	it('shows the same step on reload mid-step, from the subscribe snapshot', () => {
		render([delegated()]);
		handleThreadSnapshot({
			type: 'session_snapshot', ...turn('worker'), started_at: THREE_MIN_AGO,
			visible_root_thread_id: 'owner',
			state: {
				reasoning: '', content: '', seq: 2,
				active_tools: [{ id: 't1', name: 'read_file', status: 'running', phrase: READING }],
			},
		});
		flushSync();

		expect(subline('Delegated step')).toBe(`Working 3 min · ${READING}`);
	});

	it('reads an unknown step as Working on reload, timed from its durable start', () => {
		render([delegated()]);
		handleThreadSnapshot({
			type: 'session_snapshot', ...turn('worker'), started_at: THREE_MIN_AGO, state: null,
		});
		flushSync();

		expect(subline('Delegated step')).toBe('Working 3 min');
	});
});
