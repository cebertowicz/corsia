import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';
import { threadActivity } from './activity';
import { activeThreadId } from './threads';
import { threadPlanStates } from './threadPlanStates';
import { _resetTurnStateForTests, applyLifecycle } from '../ws/turnState';

const entry = (has_update: boolean) => ({
	state: 'idle' as const, label_key: null, paused: false, attention_items: [], has_update,
});

afterEach(() => {
	threadPlanStates.set({});
	activeThreadId.set(null);
	_resetTurnStateForTests();
	localStorage.clear();
});

describe('threadActivity update state', () => {
	it('reads done only from the server has_update, never from local storage', () => {
		localStorage.setItem('corsia_unread', JSON.stringify(['stale-local']));
		threadPlanStates.set({ updated: entry(true), seen: entry(false) });

		expect(get(threadActivity)).toEqual({ updated: 'done' });
	});

	it('never marks the thread in front of the user as done', () => {
		threadPlanStates.set({ updated: entry(true) });
		activeThreadId.set('updated');

		expect(get(threadActivity)).toEqual({});
	});

	it('lets a running Turn outrank the update', () => {
		threadPlanStates.set({ updated: entry(true) });
		applyLifecycle('updated', { turnId: 'turn-1', turnOrder: 1 });

		expect(get(threadActivity)).toEqual({ updated: 'active' });
	});
});
