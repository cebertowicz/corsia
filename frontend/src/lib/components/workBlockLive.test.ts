import { describe, expect, it } from 'vitest';

import type { ActiveTool } from '$lib/stores/threads';
import type { SubagentRef } from '$lib/stores/subagents';
import type { ThreadTurnState } from '$lib/ws/turnState';

import { workBlockLive } from './workBlockLive';

const idle: ThreadTurnState = {
	turnId: null, turnOrder: null, phase: 'idle', pendingInputIds: [], visibleRootThreadId: null, startedAt: null,
};

function running(id: string, root: string | null, startedAt: number): [string, ThreadTurnState] {
	return [id, { ...idle, turnId: `${id}-turn`, turnOrder: 1, phase: 'active', visibleRootThreadId: root, startedAt }];
}

const tools = (threadId: string, phrase: string): Map<string, ActiveTool[]> => new Map([[threadId, [
	{ id: `${threadId}-tool`, name: 'read_file', hint: '', phrase, status: 'running' },
]]]);
const subagent = (id: string, name = 'Indexer'): Map<string, SubagentRef> => new Map([[id, {
	childThreadId: id, parentThreadId: 'parent', name, provider: 'codex', status: 'running',
}]]);

describe('workBlockLive', () => {
	it('uses the visible thread’s own running tool and accent state', () => {
		const live = workBlockLive('parent', new Map([running('parent', null, 10)]), tools('parent', 'Reading files'), new Map());
		expect(live).toMatchObject({ active: true, ownRunning: true, actorName: null, previewFromDescendant: false });
		expect(live.step?.phrase).toBe('Reading files');
	});

	it('rolls a descendant into its parent when the parent is idle', () => {
		const live = workBlockLive('parent', new Map([running('child', 'parent', 20)]), tools('child', 'Running tests'), subagent('child'));
		expect(live).toMatchObject({ active: true, ownRunning: false, actorName: 'Indexer', previewFromDescendant: true, descendantIds: ['child'] });
		expect(live.step?.phrase).toBe('Running tests');
	});

	it('keeps the parent’s current tool ahead of a child activity line', () => {
		const states = new Map([running('parent', null, 10), running('child', 'parent', 20)]);
		const live = workBlockLive('parent', states, new Map([...tools('parent', 'Writing code'), ...tools('child', 'Running tests')]), subagent('child'));
		expect(live).toMatchObject({ ownRunning: true, actorName: null, previewFromDescendant: false });
		expect(live.step?.phrase).toBe('Writing code');
	});

	it('shows the newest child when the parent has no tool activity yet', () => {
		const states = new Map([running('parent', null, 10), running('older', 'parent', 20), running('newer', 'parent', 30)]);
		const live = workBlockLive('parent', states, tools('newer', 'Checking UI'), new Map([
			...subagent('older', 'Older'), ...subagent('newer', 'Newer'),
		]));
		expect(live).toMatchObject({ ownRunning: true, actorName: 'Newer', previewFromDescendant: true });
		expect(live.step?.threadId).toBe('newer');
	});

	it('keeps the block active after the parent turn has ended while its child runs', () => {
		const live = workBlockLive('parent', new Map([['parent', idle], running('child', 'parent', 20)]), tools('child', 'Verifying'), subagent('child'));
		expect(live).toMatchObject({ active: true, ownRunning: false, actorName: 'Indexer' });
	});
});
