import { get } from 'svelte/store';
import { afterEach, expect, it } from 'vitest';
import { composerSelectionFor } from './composerSelection';
import {
	_resetThreadModelRequestsForTests, _resetThreadModelTruthForTests,
	activeModelId, beginThreadModelSwitch, finishThreadModelSwitch, threadModelRequests,
} from './models';
import { activeThreadId, reasoningEffort, threads } from './threads';
import { subagentMeta, subagentThreadRows } from './subagents';

afterEach(() => {
	_resetThreadModelRequestsForTests();
	_resetThreadModelTruthForTests();
	subagentMeta.set(new Map());
	subagentThreadRows.set(new Map());
});

it('only the matching identity may settle a request, independently for each thread', () => {
	const a = beginThreadModelSwitch('a', 'codex/next')!;
	const b = beginThreadModelSwitch('b', 'claude-code/opus')!;
	expect(beginThreadModelSwitch('a', 'other')).toBeNull();
	finishThreadModelSwitch('a', Symbol('stale'));
	expect(get(threadModelRequests).get('a')?.identity).toBe(a);
	finishThreadModelSwitch('a', a);
	const replacement = beginThreadModelSwitch('a', 'newer')!;
	finishThreadModelSwitch('a', a);
	expect(get(threadModelRequests).get('a')?.identity).toBe(replacement);
	finishThreadModelSwitch('b', b);
	expect(get(threadModelRequests).has('b')).toBe(false);
});

it.each(['same', 'other', 'never-bound'].flatMap((vendor) =>
	['idle', 'running', 'held'].map((state) => [vendor, state]),
))('projects progress for %s vendor / %s without changing accepted selection or Turn fields', (vendor, state) => {
	const row = {
		id: 'matrix', model: 'codex/current', reasoning_effort: 'high',
		active_turn_id: state === 'running' ? 'current-turn' : null,
		execution_epoch: 7, provider_continuation: 'current-continuation',
		delivery_error: state === 'held' ? 'usage_limit' : null,
	};
	threads.set([row as never]);
	activeThreadId.set('matrix');
	activeModelId.set(row.model);
	reasoningEffort.set('high');
	const target = vendor === 'same' ? 'codex/next' : vendor === 'other' ? 'claude-code/opus' : 'omlx/fresh';
	const identity = beginThreadModelSwitch('matrix', target)!;
	const selection = get(composerSelectionFor)();
	expect(selection.switching).toEqual({ pending_model: target, switch_status: 'preparing', switch_error: null });
	expect(selection.modelId).toBe('codex/current');
	expect(selection.effort).toBe('high');
	expect(get(threads)).toEqual([row]);
	finishThreadModelSwitch('matrix', identity);
	expect(get(composerSelectionFor)().switching).toBe(row);
});
