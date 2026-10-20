import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import { _resetThreadModelTruthForTests, acceptThreadModelTruth, activeModelId, threadModelFor } from '$lib/stores/models';
import { composerSelectionFor } from '$lib/stores/composerSelection';
import { subagentMeta } from '$lib/stores/subagents';
import { activeThreadId, threads } from '$lib/stores/threads';
import { handleMessage } from './handlers';

const resetHeartbeat = () => {};
const worker = () => {
	const ref = get(subagentMeta).get('worker');
	return ref && { ...ref.thread, model: threadModelFor('worker') };
};

// A worker is not in the thread list: its composer reads the worker's own row.
beforeEach(() => {
	_resetThreadModelTruthForTests();
	acceptThreadModelTruth('worker', 'model/before', '2026-10-20T09:59:00.000000');
	activeThreadId.set('parent');
	activeModelId.set('model/parent');
	threads.set([{ id: 'parent', model: 'model/parent', updated_at: '2026-10-20T10:00:00.000000' } as never]);
	subagentMeta.set(new Map([['worker', {
		childThreadId: 'worker', parentThreadId: 'parent', name: 'Worker', provider: 'cc', status: 'running', model: 'provider-report-at-start',
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

	it("takes the worker's model from its subagent_meta frame when none is held", () => {
		_resetThreadModelTruthForTests();
		handleMessage({
			type: 'subagent_meta', thread_id: 'worker', parent_thread_id: 'parent', name: 'Worker',
			provider: 'cc', live: true, model: 'model/from-meta',
		}, resetHeartbeat);

		expect(worker()?.model).toBe('model/from-meta');
	});

	it("keeps the selected model when the provider reports its own name at start", () => {
		handleMessage({ type: 'subagent_init', thread_id: 'worker', model: 'claude-opus-raw-name' }, resetHeartbeat);

		expect(worker()?.model).toBe('model/before');
		expect(get(composerSelectionFor)('worker').modelId).toBe('model/before');
	});

	it("never lets a subagent_meta frame replace a model already accepted", () => {
		handleMessage({
			type: 'subagent_meta', thread_id: 'worker', parent_thread_id: 'parent', name: 'Worker',
			provider: 'cc', live: true, model: 'model/stale',
		}, resetHeartbeat);

		expect(worker()?.model).toBe('model/before');
	});
});
