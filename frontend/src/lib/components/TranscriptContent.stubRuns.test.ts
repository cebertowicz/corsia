import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { THREAD, human, said, work } from '$lib/__tests__/fixtures/focusRuns';
import { reactiveProps } from '$lib/__tests__/fixtures/reactiveProps.svelte';
import type { RunLoad, RunStub } from '$lib/focusRuns';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { PAUSED_AT_USAGE_LIMIT, PLAN_LIMIT_REASON } from '$lib/stores/overageSetting';
import TranscriptContent from './TranscriptContent.svelte';
import { planLimitPauseDescriptors, type PlanLimitPauseDescriptor } from './planLimitPause';
import type { TurnSummaries } from './transcriptRunLabel';
import { buildTranscriptPresentation } from './messageListModel';
import { _resetLiveStepForTests } from './plans/planLiveStep';

const stub: RunStub = {
	firstId: 'm2', lastId: 'm9', firstSeq: 2, lastSeq: 9, count: 8, turnIds: ['t1'], weight: 8, kinds: ['step'], continuesOlder: false,
};
const load = (over: Partial<RunLoad> = {}): RunLoad => ({
	oldestId: null, hasMore: true, loading: false, error: false, coveredFirstId: 'm2', ...over,
});

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(runLoads: Record<string, RunLoad>, loaded = false, turnSummaries = {} as TurnSummaries) {
	const rows = [
		human('ask', 1, '10:00:00', 't1', '10:00:00'),
		...(loaded ? [work('w9', 9, '10:09:00')] : []),
		said('reply', 10, '10:10:00'),
	];
	const onLoadRun = vi.fn();
	const props = reactiveProps({
		entries: buildTranscriptPresentation(rows, true, null, undefined, [stub]),
		runLoads, onLoadRun,
		turnSummaries, pauses: [] as PlanLimitPauseDescriptor[], renderedStreaming: false, historyComplete: true,
		expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
		onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {}, threadId: THREAD,
	});
	component = mount(TranscriptContent, { target: host, props });
	flushSync();
	return { onLoadRun, props };
}
const toggle = () => host.querySelector<HTMLButtonElement>('.fold-toggle')!;
const open = () => { toggle().click(); flushSync(); };

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetTurnStateForTests();
	_resetLiveStepForTests();
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('a collapsed run the page left unloaded', () => {
	it('is labelled from its stub and reads nothing until it is opened', () => {
		const { onLoadRun } = render({});

		expect(toggle().textContent).toContain('Worked · 8 steps');
		expect(host.querySelector('.fold-details')).toBeNull();
		expect(onLoadRun).not.toHaveBeenCalled();
	});

	it('starts reading when opened, and shows that it is loading', () => {
		const { onLoadRun } = render({});

		open();

		expect(onLoadRun).toHaveBeenCalledWith(stub);
		expect(host.querySelector('.fold-state')?.textContent).toContain('Loading');
	});

	it('shows a failed read inline with a retry that reads again', () => {
		const { onLoadRun } = render({ m9: load({ error: true }) });
		open();
		onLoadRun.mockClear();

		const retry = host.querySelector<HTMLButtonElement>('.fold-error .fold-action')!;
		expect(host.querySelector('.fold-error')?.textContent).toContain("Couldn't load this work");
		retry.click();

		expect(onLoadRun).toHaveBeenCalledWith(stub);
	});

	it('offers the older rows of a large run once its newest page is in', () => {
		const { onLoadRun } = render({ m9: load({ oldestId: 'm9' }) }, true);
		open();
		onLoadRun.mockClear();

		expect(host.querySelector('.fold-state')).toBeNull();
		const more = host.querySelector<HTMLButtonElement>('.fold-details > .fold-action')!;
		expect(more.textContent).toContain('Show earlier work');
		expect(host.querySelectorAll('.fold-details .message')).toHaveLength(1);
		more.click();

		expect(onLoadRun).toHaveBeenCalledWith(stub);
	});

	it('shows no control once the whole range is read, and reads a stub that grew older again', () => {
		const { props } = render({ m9: load({ hasMore: false, oldestId: 'm2' }) }, true);
		open();
		expect(host.querySelector('.fold-details > .fold-action')).toBeNull();
		expect(host.querySelector('.fold-state')).toBeNull();

		props.entries = buildTranscriptPresentation(
			[human('ask', 1, '10:00:00', 't1', '10:00:00'), work('w9', 9, '10:09:00'), said('reply', 10, '10:10:00')],
			true, null, undefined, [{ ...stub, firstId: 'm1', firstSeq: 1 }],
		);
		flushSync();

		expect(host.querySelector('.fold-details > .fold-action')?.textContent).toContain('Show earlier work');
	});

	it('starts reading a run that is already open when its stub arrives', () => {
		const { onLoadRun, props } = render({});
		open();
		onLoadRun.mockClear();
		// A newer page loads and the same stub re-enters the list: the run stays open and is not re-read.
		props.entries = buildTranscriptPresentation(
			[human('ask', 1, '10:00:00', 't1', '10:00:00'), said('reply', 10, '10:10:00')], true, null, undefined, [stub],
		);
		props.runLoads = { m9: load({ loading: true }) };
		flushSync();

		expect(onLoadRun).not.toHaveBeenCalled();
	});
});

describe('a plan-limit pause whose Turn sits only in an unloaded stub', () => {
	const stopped = { t1: { status: 'cancelled', cause: PLAN_LIMIT_REASON } } as unknown as TurnSummaries;

	it('shows the pause marker right after the stub without reading its rows', () => {
		const { onLoadRun } = render({}, false, stopped);

		const section = host.querySelector('.transcript-fold')!;
		const marker = section.querySelector('.plan-limit-paused');
		expect(marker?.textContent).toBe(PAUSED_AT_USAGE_LIMIT);
		expect(marker!.previousElementSibling).toBe(toggle());
		expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(1);
		expect(onLoadRun).not.toHaveBeenCalled();
	});

	it('shows no marker for a Turn that did not stop at the plan limit', () => {
		render({}, false, { t1: { status: 'completed', cause: null } } as unknown as TurnSummaries);

		expect(host.querySelector('.plan-limit-paused')).toBeNull();
	});

	it('does not mark a Turn twice once its rows are loaded and the loaded derivation owns it', () => {
		const { props } = render({ m9: load({ hasMore: false, oldestId: 'm2' }) }, true, stopped);
		const rows = [
			human('ask', 1, '10:00:00', 't1', '10:00:00'), work('w9', 9, '10:09:00'), said('reply', 10, '10:10:00'),
		];
		props.pauses = planLimitPauseDescriptors(rows as never, stopped);
		flushSync();

		expect(host.querySelectorAll('.plan-limit-paused')).toHaveLength(1);
	});
});
