import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { composerSelectionFor } from './composerSelection';
import { _resetThreadModelTruthForTests, acceptThreadModelTruth, seedThreadModelTruth, type Model } from './models';
import { activeModelId, models, threadCatalogs, workspaceModels } from './modelsState';
import { subagentMeta, subagentThreadRows } from './subagents';
import { activeThreadId, reasoningEffort, threads } from './threads';

beforeEach(() => {
	_resetThreadModelTruthForTests();
	acceptThreadModelTruth('worker', 'model/worker', '2026-10-20T10:00:00.000000');
	activeThreadId.set('open');
	activeModelId.set('model/open');
	reasoningEffort.set('high');
	threads.set([
		{ id: 'open', model: 'model/open', reasoning_effort: 'high' } as never,
		{ id: 'other', model: 'model/other', reasoning_effort: 'low' } as never,
	]);
	subagentMeta.set(new Map([['worker', {
		childThreadId: 'worker', parentThreadId: 'open', name: 'Worker', provider: 'cc', status: 'running',
	}]]));
	subagentThreadRows.set(new Map([['worker', { reasoning_effort: 'none', project_id: 'project-1' }]]));
});

afterEach(() => {
	activeThreadId.set(null);
	activeModelId.set(null);
	reasoningEffort.set(null);
	threads.set([]);
	subagentMeta.set(new Map());
	subagentThreadRows.set(new Map());
	models.set([]);
	workspaceModels.set([]);
	threadCatalogs.set(new Map());
	_resetThreadModelTruthForTests();
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

	it("reads a worker's model from the thread truth and its effort from its row, never the open thread's", () => {
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

	it("re-reads a worker's model when only the thread truth changed", () => {
		threads.set([{ id: 'open', model: 'model/open', updated_at: '2026-10-20T09:00:00' } as never]);
		const seen: Array<string | null> = [];
		const stop = composerSelectionFor.subscribe((read) => seen.push(read('worker').modelId));

		acceptThreadModelTruth('worker', 'model/next', '2026-10-20T10:05:00.000000');
		stop();

		expect(seen[0]).toBe('model/worker');
		expect(seen.at(-1)).toBe('model/next');
	});

	it("follows a worker's model as the server reports it, until a timed model outranks it", () => {
		_resetThreadModelTruthForTests();
		seedThreadModelTruth('worker', 'model/first');
		expect(get(composerSelectionFor)('worker').modelId).toBe('model/first');

		seedThreadModelTruth('worker', 'model/second');
		expect(get(composerSelectionFor)('worker').modelId).toBe('model/second');

		threads.set([{ id: 'open', model: 'model/open', updated_at: '2026-10-20T09:00:00' } as never]);
		acceptThreadModelTruth('worker', 'model/accepted', '2026-10-20T10:06:00.000000');
		seedThreadModelTruth('worker', 'model/stale-report');
		expect(get(composerSelectionFor)('worker').modelId).toBe('model/accepted');
	});

	it("gives the open thread the open catalog and a worker its own, unknown until read", () => {
		const row = (id: string, status: string) => ({ id, status, litellm_model: id }) as unknown as Model;
		models.set([row('model/open', 'ready')]);
		workspaceModels.set([row('model/worker', 'ready')]);

		expect(get(composerSelectionFor)('open').models.map((m) => m.id)).toEqual(['model/open']);
		expect(get(composerSelectionFor)('worker').models).toEqual([
			expect.objectContaining({ id: 'model/worker', status: 'unavailable', status_reason: 'status_unknown' }),
		]);

		threadCatalogs.set(new Map([['worker', [row('model/worker', 'ready')]]]));
		expect(get(composerSelectionFor)('worker').models).toEqual([expect.objectContaining({ status: 'ready' })]);
	});
});
