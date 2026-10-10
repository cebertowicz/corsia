import { get } from 'svelte/store';
import { describe, expect, it, beforeEach } from 'vitest';

import { descendantRunning } from './activity';
import {
	_resetTurnStateForTests,
	applyLifecycle,
	applyTerminal,
	restoreAuthTurns,
} from '../ws/turnState';

beforeEach(() => {
	_resetTurnStateForTests();
});

describe('descendantRunning', () => {
	it('marks the visible root while a descendant turn runs', () => {
		applyLifecycle('sub-cc', { turnId: 'turn-cc', turnOrder: 1 }, 'parent-1');

		expect(get(descendantRunning).has('parent-1')).toBe(true);
		expect(get(descendantRunning).has('sub-cc')).toBe(false);
	});

	it('never marks the thread running its own turn', () => {
		applyLifecycle('chat-1', { turnId: 'turn-own', turnOrder: 1 }, 'chat-1');

		expect(get(descendantRunning).has('chat-1')).toBe(false);
	});

	it('ignores a turn whose lineage is unknown', () => {
		applyLifecycle('sub-cc', { turnId: 'turn-cc', turnOrder: 1 });

		expect(get(descendantRunning).size).toBe(0);
	});

	it('clears when the descendant turn ends, and restores from the auth snapshot', () => {
		applyLifecycle('sub-cc', { turnId: 'turn-cc', turnOrder: 1 }, 'parent-1');
		expect(get(descendantRunning).has('parent-1')).toBe(true);

		applyTerminal('sub-cc', { turnId: 'turn-cc', turnOrder: 1 });
		expect(get(descendantRunning).has('parent-1')).toBe(false);

		// Reconnect: the auth snapshot is the recovery path for the same truth.
		restoreAuthTurns([
			{ threadId: 'sub-cc', turnId: 'turn-cc', turnOrder: 1, visibleRootThreadId: 'parent-1' },
		]);
		expect(get(descendantRunning).has('parent-1')).toBe(true);
	});

	it('keeps gray truth while several descendants run, then drops with the last', () => {
		applyLifecycle('sub-a', { turnId: 'turn-a', turnOrder: 1 }, 'parent-1');
		applyLifecycle('sub-b', { turnId: 'turn-b', turnOrder: 2 }, 'parent-1');
		expect(get(descendantRunning).size).toBe(1);

		applyTerminal('sub-a', { turnId: 'turn-a', turnOrder: 1 });
		expect(get(descendantRunning).has('parent-1')).toBe(true);

		applyTerminal('sub-b', { turnId: 'turn-b', turnOrder: 2 });
		expect(get(descendantRunning).size).toBe(0);
	});
});
