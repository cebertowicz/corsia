import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '$lib/stores/threads';
import { handleMessage } from './handlers';
import { awaitFirstRunOpening, cancelFirstRunOpening, _resetFirstRunOpeningRevealForTests } from './firstRunOpeningReveal';
import { _getActiveTypewriterCount, _resetTypewritersForTests, cancelThreadTypewriters } from './typewriter';
import { _resetTurnStateForTests, applyLifecycle } from './turnState';

const thread = 'opening-thread';
const content = 'Hi Pat.\n\nI am CORSIA. Here is the plan for finishing setup.';
const opening = { id: `first-run-opening:${thread}`, role: 'assistant', content,
	created_at: '2026-10-22T00:00:00Z', seq: 1,
	metadata: { for_user: true, first_run_opening: true, turn_unbound_reason: 'server_scripted' } };
const receive = () => handleMessage({ type: 'message_added', thread_id: thread, message: opening }, () => {});
const row = () => get(messages).find(message => message.id === opening.id)!;

beforeEach(() => {
	vi.useFakeTimers();
	vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
	messages.set([]); activeThreadId.set(thread);
	_resetFirstRunOpeningRevealForTests(); _resetTypewritersForTests(); _resetTurnStateForTests();
});
afterEach(() => {
	_resetTypewritersForTests(); _resetFirstRunOpeningRevealForTests(); _resetTurnStateForTests();
	messages.set([]); activeThreadId.set(null); vi.useRealTimers(); vi.unstubAllGlobals();
});

describe('local first-pick opening', () => {
	it('starts on live arrival, advances by words, retains full content, and never replays', () => {
		awaitFirstRunOpening(thread); receive();
		expect(row().content).toBe(content);
		expect(row().typewriterPos).toBe(0);
		vi.advanceTimersByTime(90);
		expect(content.slice(0, row().typewriterPos)).toBe('Hi ');
		vi.advanceTimersByTime(90);
		expect(content.slice(0, row().typewriterPos)).toBe('Hi Pat.\n\n');
		// A turn can finish before the scripted reveal: its terminal state must not end it.
		applyLifecycle(thread, { turnId: 'first', turnOrder: 1 });
		cancelThreadTypewriters(thread, { turnId: 'first', turnOrder: 1 });
		expect(_getActiveTypewriterCount(thread)).toBe(1);
		receive(); expect(_getActiveTypewriterCount(thread)).toBe(1);
		vi.advanceTimersByTime(5_000);
		expect(row().typewriterPos).toBeUndefined();
		receive(); expect(row().typewriterPos).toBeUndefined();
		expect(_getActiveTypewriterCount(thread)).toBe(0);
	});
	it.each(['other device', 'history', 'cancelled pick', 'reduced motion', 'hidden thread'])('%s shows the whole opening', scenario => {
		if (scenario !== 'other device') awaitFirstRunOpening(thread);
		if (scenario === 'history') messages.set([{ ...opening, thread_id: thread }] as never);
		if (scenario === 'cancelled pick') cancelFirstRunOpening(thread);
		if (scenario === 'reduced motion') vi.stubGlobal('matchMedia', () => ({ matches: true }));
		if (scenario === 'hidden thread') activeThreadId.set('another-thread');
		receive();
		expect(_getActiveTypewriterCount(thread)).toBe(0);
		if (scenario !== 'hidden thread') expect(row().typewriterPos).toBeUndefined();
	});
	it('navigation finishes the decoration and revisiting does not start it again', () => {
		awaitFirstRunOpening(thread); receive(); vi.advanceTimersByTime(90);
		activeThreadId.set('another-thread'); vi.advanceTimersByTime(90);
		expect(row().typewriterPos).toBeUndefined();
		activeThreadId.set(thread); receive();
		expect(row().typewriterPos).toBeUndefined();
	});
});
