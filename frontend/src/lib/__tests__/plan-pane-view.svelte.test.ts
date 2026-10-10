import { flushSync } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OperatorEnvelope, OperatorNode } from '$lib/api/plans';
import { IDLE_MS, createPaneView } from '$lib/components/plans/paneView.svelte';
import { PAUSE_MS } from '$lib/components/plans/foldWave';
import { token } from '$lib/stores/auth';
import { lastUserInputAt } from '$lib/userInput';
import { node } from './fixtures/planTreeNode';

/**
 * The pane folds new finished rows and reports them seen once the freshly
 * loaded plan has been on screen, in a visible page the user has touched in
 * the last `IDLE_MS`, for `PAUSE_MS`. No presence claim is involved; a cached
 * copy, a hidden page, an open pane nobody is at, or a view with nothing new
 * reports nothing.
 */

const AS_OF = '2026-09-24T12:00:00.000000+00:00';
const LATER = '2026-09-24T12:05:00.000000+00:00';
const DONE = node({ id: 'done', state: 'accepted', folded: true, last_transition_at: '2026-09-24T11:00:00Z' });

function envelope(nodes: OperatorNode[], marks: Partial<OperatorEnvelope> = {}): OperatorEnvelope {
	return { as_of: AS_OF, plans: [{ nodes }], seen_through: null, ...marks } as OperatorEnvelope;
}

let fetchMock: ReturnType<typeof vi.fn>;
let stop: (() => void) | null = null;
let thread = 0;
let visibility: DocumentVisibilityState = 'visible';
beforeEach(() => {
	vi.useFakeTimers();
	// The page clock outlives each test; start past its last input, so nobody
	// is at the page until this test says so.
	vi.setSystemTime(Math.max(Date.now(), lastUserInputAt()) + 10 * IDLE_MS);
	fetchMock = vi.fn(async () => new Response('{}', { status: 200 }));
	vi.stubGlobal('fetch', fetchMock);
	token.set('test-bearer');
	thread += 1;
	visibility = 'visible';
	Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
});
afterEach(() => {
	stop?.();
	stop = null;
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

function view(payload: OperatorEnvelope | null, lastFreshAt: number | null) {
	const threadId = `thread-${thread}`;
	const state = $state({ payload, lastFreshAt });
	let handle!: ReturnType<typeof createPaneView>;
	stop = $effect.root(() => {
		handle = createPaneView({
			threadId: () => threadId,
			payload: () => state.payload,
			lastFreshAt: () => state.lastFreshAt,
		});
	});
	flushSync();
	return { threadId, state, get handle() { return handle; } };
}

/** The user's hand on the page, as the click that opens the pane. */
function touch() {
	window.dispatchEvent(new Event('pointerdown'));
	flushSync();
}

function wait(ms: number) {
	vi.advanceTimersByTime(ms);
	flushSync();
}

const reports = () => fetchMock.mock.calls.map(([url, init]) => ({
	url: String(url), body: JSON.parse(String((init as RequestInit).body)),
}));

describe('createPaneView', () => {
	it('folds and reports after the pause on a fresh load, with no presence claim', () => {
		touch();
		const v = view(envelope([DONE]), Date.now() + 1);
		wait(PAUSE_MS - 1);
		expect(v.handle.view.seenThrough).toBeNull();
		expect(fetchMock).not.toHaveBeenCalled();
		wait(1);
		expect(v.handle.view.seenThrough).toBe(AS_OF);
		expect(reports()).toEqual([{ url: `/api/plans/operator/threads/${v.threadId}/seen`, body: { seen_through: AS_OF } }]);
	});

	it('does not fold or report while an open, visible pane has no one at it', () => {
		const v = view(envelope([DONE]), Date.now() + 1);
		wait(IDLE_MS * 2);
		expect(v.handle.view.seenThrough).toBeNull();
		expect(fetchMock).not.toHaveBeenCalled();
		touch();
		wait(PAUSE_MS - 1);
		expect(fetchMock).not.toHaveBeenCalled();
		wait(1);
		expect(v.handle.view.seenThrough).toBe(AS_OF);
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('stops counting the pane as looked at once input is older than IDLE_MS', () => {
		touch();
		const v = view(envelope([DONE]), Date.now() + 1);
		wait(PAUSE_MS);
		expect(v.handle.view.seenThrough).toBe(AS_OF);
		wait(IDLE_MS);
		const late = node({ id: 'late', state: 'accepted', folded: true, last_transition_at: '2026-09-24T12:03:00Z' });
		v.state.payload = envelope([DONE, late], { as_of: LATER, seen_through: AS_OF });
		flushSync();
		wait(PAUSE_MS * 2);
		expect(v.handle.view.seenThrough).toBe(AS_OF);
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('waits for a hidden page to become visible, then pauses', () => {
		touch();
		visibility = 'hidden';
		const v = view(envelope([DONE]), Date.now() + 1);
		wait(PAUSE_MS * 3);
		expect(fetchMock).not.toHaveBeenCalled();
		visibility = 'visible';
		document.dispatchEvent(new Event('visibilitychange'));
		flushSync();
		wait(PAUSE_MS - 1);
		expect(fetchMock).not.toHaveBeenCalled();
		wait(1);
		expect(v.handle.view.seenThrough).toBe(AS_OF);
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('does not fold or report on a copy loaded before the pane opened', () => {
		touch();
		const v = view(envelope([DONE]), Date.now() - 60_000);
		wait(PAUSE_MS);
		expect(v.handle.view.seenThrough).toBeNull();
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('reports nothing when the mark already covers every finished row', () => {
		touch();
		view(envelope([DONE], { seen_through: AS_OF }), Date.now() + 1);
		wait(PAUSE_MS * 2);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('gives a row that finishes while the pane is open its own pause and report', () => {
		touch();
		const v = view(envelope([DONE]), Date.now() + 1);
		wait(PAUSE_MS);
		const late = node({ id: 'late', state: 'accepted', folded: true, last_transition_at: '2026-09-24T12:03:00Z' });
		v.state.payload = envelope([DONE, late], { as_of: LATER, seen_through: AS_OF });
		flushSync();
		expect(v.handle.view.seenThrough).toBe(AS_OF);
		wait(PAUSE_MS);
		expect(v.handle.view.seenThrough).toBe(LATER);
		expect(reports().map((report) => report.body)).toEqual([{ seen_through: AS_OF }, { seen_through: LATER }]);
	});
});
