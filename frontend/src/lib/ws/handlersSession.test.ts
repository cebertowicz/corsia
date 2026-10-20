import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import { _resetThreadModelTruthForTests, activeModelId } from '$lib/stores/models';
import { subagentMeta } from '$lib/stores/subagents';
import { activeThreadId, threads } from '$lib/stores/threads';
import { handleMessage } from './handlers';

const resetHeartbeat = () => {};
const worker = () => get(subagentMeta).get('worker')?.thread;

// A worker is not in the thread list: its composer reads the worker's own row.
beforeEach(() => {
	_resetThreadModelTruthForTests();
	activeThreadId.set('parent');
	activeModelId.set('model/parent');
	threads.set([{ id: 'parent', model: 'model/parent', updated_at: '2026-10-20T10:00:00.000000' } as never]);
	subagentMeta.set(new Map([['worker', {
		childThreadId: 'worker', parentThreadId: 'parent', name: 'Worker', provider: 'cc', status: 'running',
		thread: { model: 'model/before' },
	}]]));
});

afterEach(() => {
	activeThreadId.set(null);
	activeModelId.set(null);
	threads.set([]);
	subagentMeta.set(new Map());
});

describe('session_updated for a worker', () => {
	it("lands a committed model switch on the worker's row, not the open thread's", () => {
		handleMessage({
			type: 'session_updated', thread_id: 'worker', model: 'model/after',
			pending_model: null, switch_status: null, switch_error: null, switch_operation_id: null,
			updated_at: '2026-10-20T10:00:01.000000',
		}, resetHeartbeat);

		expect(worker()).toMatchObject({ model: 'model/after', pending_model: null, switch_status: null });
		expect(get(threads)[0].model).toBe('model/parent');
	});

	it("lands a pending or failed switch on the worker's row", () => {
		handleMessage({
			type: 'session_updated', thread_id: 'worker', pending_model: 'model/next',
			switch_status: 'failed', switch_error: 'no room', switch_operation_id: 'op-1',
			updated_at: '2026-10-20T10:00:02.000000',
		}, resetHeartbeat);

		expect(worker()).toMatchObject({
			model: 'model/before', pending_model: 'model/next', switch_status: 'failed', switch_error: 'no room',
		});
	});

	it("takes the worker's model from its subagent_meta frame", () => {
		handleMessage({
			type: 'subagent_meta', thread_id: 'worker', parent_thread_id: 'parent', name: 'Worker',
			provider: 'cc', live: true, model: 'model/from-meta',
		}, resetHeartbeat);

		expect(worker()?.model).toBe('model/from-meta');
	});
});
