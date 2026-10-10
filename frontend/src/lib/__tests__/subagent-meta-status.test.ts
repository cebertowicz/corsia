import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';

import { handleMessage } from '../ws/handlers';
import { subagentMeta } from '../stores/subagents';

function noop() {}

describe('subagent metadata status reconciliation', () => {
	beforeEach(() => {
		subagentMeta.set(new Map());
	});

	it('treats non-live subscribe metadata as open, not terminal done', () => {
		handleMessage(
			{
				type: 'subagent_meta',
				parent_thread_id: 'parent-thread',
				thread_id: 'child-thread',
				name: 'Smoke worker',
				provider: 'cc',
				live: false,
			},
			noop,
		);

		expect(get(subagentMeta).get('child-thread')).toMatchObject({
			parentThreadId: 'parent-thread',
			childThreadId: 'child-thread',
			name: 'Smoke worker',
			status: 'idle',
		});
	});

	it('routes spawned and status frames by child_thread_id', () => {
		handleMessage(
			{
				type: 'subagent_spawned',
				thread_id: 'parent-thread',
				child_thread_id: 'child-thread',
				name: 'Contract worker',
				provider: 'codex',
			},
			noop,
		);
		handleMessage(
			{
				type: 'subagent_status',
				child_thread_id: 'child-thread',
				status: 'ready',
			},
			noop,
		);

		expect(get(subagentMeta).get('child-thread')).toMatchObject({
			parentThreadId: 'parent-thread',
			childThreadId: 'child-thread',
			name: 'Contract worker',
			provider: 'codex',
			status: 'ready',
		});
	});
});
