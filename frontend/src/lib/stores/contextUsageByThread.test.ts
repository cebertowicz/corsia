import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	clearAllContextUsage, contextPreviewByThread, contextReading, ensureContextPreview,
	previewIdentity, recordMeasuredUsage,
} from './contextUsageByThread';
import { fetchNextTurnPreview } from './nextTurnPreview';

vi.mock('./nextTurnPreview', () => ({ fetchNextTurnPreview: vi.fn() }));
const fetchPreview = vi.mocked(fetchNextTurnPreview);

function usage(total: number) {
	return { total_used: total, model_context_window: 1000, token_usage_available: true } as never;
}

function request(threadId: string, modelId: string, measured: ReturnType<typeof usage>) {
	return { threadId, modelId, authToken: 'test', ...previewIdentity(threadId, modelId, measured, null) };
}

afterEach(() => { clearAllContextUsage(); vi.resetAllMocks(); });

describe('preview refresh', () => {
	it('holds the previous preview during a receipt refresh and replaces it on success', async () => {
		const first = request('a', 'model-a', usage(400));
		fetchPreview.mockResolvedValueOnce(usage(300));
		await ensureContextPreview(first);
		let finish!: (value: ReturnType<typeof usage>) => void;
		fetchPreview.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
		recordMeasuredUsage('a', usage(500));
		const next = request('a', 'model-a', usage(500));
		const pending = ensureContextPreview(next);
		const loading = get(contextPreviewByThread).get('a');
		expect(loading?.loading).toBe(true);
		expect(contextReading(loading, next.owner, usage(500))?.total_used).toBe(300);
		finish(usage(600));
		await pending;
		expect(contextReading(get(contextPreviewByThread).get('a'), next.owner, usage(500))?.total_used).toBe(600);
	});

	it('does not carry a preview to another model or thread', async () => {
		fetchPreview.mockResolvedValueOnce(usage(300));
		await ensureContextPreview(request('a', 'model-a', usage(400)));
		recordMeasuredUsage('a', usage(500));
		fetchPreview.mockImplementation(() => new Promise(() => {}));
		const switched = request('a', 'model-b', usage(500));
		void ensureContextPreview(switched);
		expect(contextReading(get(contextPreviewByThread).get('a'), switched.owner, usage(500))?.total_used).toBe(500);
		const other = request('b', 'model-a', usage(200));
		void ensureContextPreview(other);
		expect(contextReading(get(contextPreviewByThread).get('b'), other.owner, usage(200))?.total_used).toBe(200);
	});
});
