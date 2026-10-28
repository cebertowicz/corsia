import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { activeThreadId } from '../stores/threads';
import type { Checkpoint } from '../stores/checkpoints';
import { dropInMemoryInstalls, refreshUsageAfterInstall, resetAppliedInstalls } from './checkpointInvalidation';
import {
	clearAllContextUsage,
	contextPreviewByThread,
	contextReading,
	ensureContextPreview,
	getContextUsage,
	previewIdentity,
	setContextUsage,
} from '../stores/contextUsageByThread';

const measured = { model_context_window: 262_144, total_used: 250_000, token_source: 'measured' } as any;
const preview = { model_context_window: 262_144, total_used: 40_000, token_source: 'estimated', preview: true };

function checkpoint(id: string, status: Checkpoint['status']): Checkpoint {
	return {
		id, status, trigger: 'manual', mode: 'warm', writerModel: 'm',
		error: null, startedAt: '2026-09-17T09:00:00Z',
	};
}

function mockPreview() {
	const fetchMock = vi.fn(async () => new Response(JSON.stringify({ preview })));
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

/** What the donut and the sheet headline show for `threadId` right now. */
function shownReading(threadId: string, latest: Checkpoint) {
	const { owner } = previewIdentity(threadId, null, getContextUsage(threadId), latest);
	return contextReading(get(contextPreviewByThread).get(threadId), owner, getContextUsage(threadId));
}

beforeEach(() => {
	clearAllContextUsage();
	activeThreadId.set('viewed');
	setContextUsage('viewed', measured);
	resetAppliedInstalls();
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('chip usage after a checkpoint install (#1562)', () => {
	it('shows the preview the sheet reads without writing it into the measured slot', async () => {
		const fetchMock = mockPreview();
		const installed = checkpoint('c1', 'installed');
		await refreshUsageAfterInstall('viewed', 'tok', [installed]);
		expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toContain('/api/threads/viewed/context-snapshot/next-turn');
		expect(getContextUsage('viewed')).toBe(measured);
		expect(get(contextPreviewByThread).get('viewed')?.usage).toEqual({ ...preview, thread_id: 'viewed' });
		expect(shownReading('viewed', installed)).toEqual({ ...preview, thread_id: 'viewed' });
	});

	it('falls back to the measured reading when a deferred preview is held', () => {
		// The state a resumable session without capacity leaves outside an
		// install: the sheet keeps the last measured reading.
		const installed = checkpoint('c1', 'installed');
		const { owner, key } = previewIdentity('viewed', null, measured, installed);
		contextPreviewByThread.set(new Map([['viewed', {
			owner, key, modelId: null, generation: 0, sourceOwner: null, usage: null, loading: false, error: '',
		}]]));

		expect(shownReading('viewed', installed)).toBe(measured);
	});

	it('waits for a fresh install preview when another read is in flight', async () => {
		const pending: Array<(value: Response) => void> = [];
		const fetchMock = vi.fn(() => new Promise<Response>(resolve => pending.push(resolve)));
		vi.stubGlobal('fetch', fetchMock);
		const beforeInstall = previewIdentity('viewed', null, measured, null);
		const initialRead = ensureContextPreview({ threadId: 'viewed', authToken: 'tok', modelId: null, ...beforeInstall });
		expect(fetchMock).toHaveBeenCalledTimes(1);

		const installed = checkpoint('c1', 'installed');
		const refreshed = refreshUsageAfterInstall('viewed', 'tok', [installed]);
		pending[0](new Response(JSON.stringify({ preview: { ...preview, total_used: 30_000 } })));
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
		pending[1](new Response(JSON.stringify({ preview })));
		await Promise.all([initialRead, refreshed]);

		expect(get(contextPreviewByThread).get('viewed')?.usage).toEqual({ ...preview, thread_id: 'viewed' });
		expect(getContextUsage('viewed')).toBe(measured);
	});

	it('restarts a same-key install read invalidated while it is in flight', async () => {
		const pending: Array<(value: Response) => void> = [];
		const fetchMock = vi.fn(() => new Promise<Response>(resolve => pending.push(resolve)));
		vi.stubGlobal('fetch', fetchMock);
		const installed = checkpoint('c1', 'installed');
		const identity = previewIdentity('viewed', null, measured, installed);
		const initialRead = ensureContextPreview({ threadId: 'viewed', authToken: 'tok', modelId: null, ...identity });
		expect(fetchMock).toHaveBeenCalledTimes(1);

		const refreshed = refreshUsageAfterInstall('viewed', 'tok', [installed]);
		pending[0](new Response(JSON.stringify({ preview: { ...preview, total_used: 30_000 } })));
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
		pending[1](new Response(JSON.stringify({ preview })));
		await Promise.all([initialRead, refreshed]);

		expect(get(contextPreviewByThread).get('viewed')?.usage).toEqual({ ...preview, thread_id: 'viewed' });
		expect(getContextUsage('viewed')).toBe(measured);
	});

	it('marks the pre-install count unavailable when the install read defers', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ preview: null }))));
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);

		expect(getContextUsage('viewed')).toEqual({
			...measured, token_usage_available: false, token_source: undefined,
		});
	});

	it('ignores checkpoints that have not installed', async () => {
		const fetchMock = mockPreview();
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'running')]);
		expect(fetchMock).not.toHaveBeenCalled();
		expect(getContextUsage('viewed')).toBe(measured);
	});

	it('applies one install once, so a later measured turn is not overwritten', async () => {
		mockPreview();
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);
		const nextTurn = { ...measured, total_used: 60_000 };
		setContextUsage('viewed', nextTurn);
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);
		expect(getContextUsage('viewed')).toBe(nextTurn);
	});

	it('retires an older preview when this install has not yet been applied', async () => {
		const installed = checkpoint('c1', 'installed');
		const { owner, key } = previewIdentity('viewed', null, measured, null);
		contextPreviewByThread.set(new Map([['viewed', {
			owner, key, modelId: null, generation: 0, sourceOwner: owner,
			usage: { ...preview, total_used: 90_000 } as never, loading: false, error: '',
		}]]));
		mockPreview();
		await refreshUsageAfterInstall('viewed', 'tok', [installed]);
		expect(shownReading('viewed', installed)?.total_used).toBe(40_000);
	});

	it('shows the measured reading again once a turn measures after the install', async () => {
		const installed = checkpoint('c1', 'installed');
		mockPreview();
		await refreshUsageAfterInstall('viewed', 'tok', [installed]);
		const nextTurn = { ...measured, total_used: 60_000, snapshot_id: 'after' };
		setContextUsage('viewed', nextTurn);
		expect(shownReading('viewed', installed)).toBe(nextTurn);
	});

	it('remembers an applied install across a reload', async () => {
		const fetchMock = mockPreview();
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		dropInMemoryInstalls();
		const laterTurn = { ...measured, total_used: 60_000 };
		setContextUsage('viewed', laterTurn);
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(getContextUsage('viewed')).toBe(laterTurn);
	});

	it('bounds the persisted install guard', async () => {
		const fetchMock = mockPreview();
		for (let index = 0; index <= 128; index += 1) {
			const threadId = `viewed-${index}`;
			activeThreadId.set(threadId);
			setContextUsage(threadId, measured);
			await refreshUsageAfterInstall(threadId, 'tok', [checkpoint(`c${index}`, 'installed')]);
		}
		expect(fetchMock).toHaveBeenCalledTimes(129);
		const stored = JSON.parse(sessionStorage.getItem('corsia.appliedCheckpointInstalls') ?? '{}');
		expect(Object.keys(stored)).toHaveLength(128);
		expect(stored['viewed-0']).toBeUndefined();
		expect(stored['viewed-128']).toBe('c128');
	});

	it('drops the preview when a turn lands while it is loading', async () => {
		const nextTurn = { ...measured, total_used: 60_000 };
		vi.stubGlobal('fetch', vi.fn(async () => {
			setContextUsage('viewed', nextTurn);
			return new Response(JSON.stringify({ preview }));
		}));
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);
		expect(getContextUsage('viewed')).toBe(nextTurn);
	});

	it('does nothing when the durable checkpoint read failed', async () => {
		const fetchMock = mockPreview();
		await refreshUsageAfterInstall('viewed', 'tok', null);
		expect(fetchMock).not.toHaveBeenCalled();
		expect(getContextUsage('viewed')).toBe(measured);
	});

	it('leaves other threads alone', async () => {
		const fetchMock = mockPreview();
		await refreshUsageAfterInstall('elsewhere', 'tok', [checkpoint('c1', 'installed')]);
		expect(fetchMock).not.toHaveBeenCalled();
		expect(getContextUsage('viewed')).toBe(measured);
	});

	it('marks the stale count unavailable when the preview read fails', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 500 })));
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);
		expect(getContextUsage('viewed')).toEqual({ ...measured, token_usage_available: false, token_source: undefined });
	});

	it('marks the stale count unavailable when the preview request throws', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c1', 'installed')]);
		expect(getContextUsage('viewed')?.token_usage_available).toBe(false);
	});

	it('reads the newest checkpoint as the install to apply', async () => {
		const fetchMock = mockPreview();
		await refreshUsageAfterInstall('viewed', 'tok', [checkpoint('c2', 'running'), checkpoint('c1', 'installed')]);
		expect(fetchMock).not.toHaveBeenCalled();
	});
});
