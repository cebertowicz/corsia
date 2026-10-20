import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { composerSelectionFor } from './composerSelection';
import { activeModelId } from './modelsState';
import { subagentMeta } from './subagents';
import { activeThreadId, reasoningEffort, threads } from './threads';

beforeEach(() => {
	activeThreadId.set('open');
	activeModelId.set('model/open');
	reasoningEffort.set('high');
	threads.set([
		{ id: 'open', model: 'model/open', reasoning_effort: 'high' } as never,
		{ id: 'other', model: 'model/other', reasoning_effort: 'low' } as never,
	]);
	subagentMeta.set(new Map([['worker', {
		childThreadId: 'worker', parentThreadId: 'open', name: 'Worker', provider: 'cc', status: 'running', model: 'model/worker',
		thread: { reasoning_effort: 'none', project_id: 'project-1' },
	}]]));
});

afterEach(() => {
	activeThreadId.set(null);
	activeModelId.set(null);
	reasoningEffort.set(null);
	threads.set([]);
	subagentMeta.set(new Map());
});

describe('composerSelectionFor', () => {
	it('reads the open thread from the global selection', () => {
		const selection = get(composerSelectionFor)('open');

		expect(selection).toMatchObject({ threadId: 'open', isOpen: true, modelId: 'model/open', effort: 'high' });
	});

	it('treats an omitted id as the open thread', () => {
		expect(get(composerSelectionFor)()).toMatchObject({ threadId: 'open', isOpen: true, modelId: 'model/open' });
	});

	it('reads a draft chat from the global selection', () => {
		activeThreadId.set(null);

		expect(get(composerSelectionFor)(null))
			.toMatchObject({ threadId: null, isOpen: true, modelId: 'model/open', effort: 'high', thread: undefined });
	});

	it("reads a worker from its own row, never the open thread's selection", () => {
		const selection = get(composerSelectionFor)('worker');

		expect(selection).toMatchObject({ threadId: 'worker', isOpen: false, modelId: 'model/worker', effort: 'none' });
		expect(selection.thread?.project_id).toBe('project-1');
	});

	it("reads another listed thread from that thread's row", () => {
		expect(get(composerSelectionFor)('other'))
			.toMatchObject({ isOpen: false, modelId: 'model/other', effort: 'low' });
	});

	it('has no model or effort for a thread whose row has not loaded', () => {
		expect(get(composerSelectionFor)('unknown'))
			.toMatchObject({ isOpen: false, thread: undefined, modelId: null, effort: null });
	});

	it('follows the open thread when it changes, and a worker becoming the open thread uses the global selection', () => {
		activeThreadId.set('worker');
		activeModelId.set('model/worker-open');

		expect(get(composerSelectionFor)('worker')).toMatchObject({ isOpen: true, modelId: 'model/worker-open' });
		expect(get(composerSelectionFor)('open')).toMatchObject({ isOpen: false, modelId: 'model/open' });
	});
});
