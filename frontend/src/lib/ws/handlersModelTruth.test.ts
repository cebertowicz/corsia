import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import {
	_resetThreadModelTruthForTests,
	activeModelId,
	reconcileThreadModelSnapshots,
} from '$lib/stores/models';
import { activeThreadId, threads } from '$lib/stores/threads';
import { handleMessage } from './handlers';
import { clearAllContextUsage, setContextUsage, getContextUsage } from '../stores/contextUsageByThread';

const resetHeartbeat = () => {};

beforeEach(() => {
	_resetThreadModelTruthForTests();
	activeThreadId.set(null);
	activeModelId.set(null);
	clearAllContextUsage();
	threads.set([]);
});

describe('session_updated model truth', () => {
	it('ignores an older model event that arrives after newer server truth', () => {
		threads.set([{
			id: 'thread-visible',
			model: 'model-before',
			updated_at: '2026-07-17T10:00:00.000000',
		} as never]);
		activeThreadId.set('thread-visible');
		activeModelId.set('model-before');

		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-visible',
			model: 'model-newest',
			updated_at: '2026-07-17T10:00:02.000000',
		}, resetHeartbeat);
		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-visible',
			model: 'model-stale',
			updated_at: '2026-07-17T10:00:01.000000',
		}, resetHeartbeat);

		expect(get(threads)[0]).toMatchObject({
			model: 'model-newest',
			updated_at: '2026-07-17T10:00:02.000000',
		});
		expect(get(activeModelId)).toBe('model-newest');
	});

	it('updates a hidden active thread without inserting it into visible threads', () => {
		activeThreadId.set('thread-hidden');
		activeModelId.set('model-before');

		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-hidden',
			model: 'model-after',
			updated_at: '2026-07-17T10:00:01.000000',
		}, resetHeartbeat);

		expect(get(threads)).toEqual([]);
		expect(get(activeModelId)).toBe('model-after');
	});

	it('keeps a newer server-list snapshot over a delayed model event', () => {
		threads.set(reconcileThreadModelSnapshots([{
			id: 'thread-loaded',
			model: 'model-from-newer-load',
			updated_at: '2026-07-17T10:00:02.000000',
		}]));

		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-loaded',
			model: 'model-from-stale-event',
			updated_at: '2026-07-17T10:00:01.000000',
		}, resetHeartbeat);

		expect(get(threads)[0]?.model).toBe('model-from-newer-load');
	});

	it('keeps a newer model event when an older server-list request resolves late', () => {
		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-loaded-late',
			model: 'model-from-newer-event',
			updated_at: '2026-07-17T10:00:02.000000',
		}, resetHeartbeat);

		threads.set(reconcileThreadModelSnapshots([{
			id: 'thread-loaded-late',
			model: 'model-from-stale-load',
			updated_at: '2026-07-17T10:00:01.000000',
		}]));

		expect(get(threads)[0]).toMatchObject({
			model: 'model-from-newer-event',
			updated_at: '2026-07-17T10:00:02.000000',
		});
	});

	it('synchronizes the active model from a newer reconnect snapshot', () => {
		activeThreadId.set('thread-reconnected');
		activeModelId.set('model-before-reconnect');

		threads.set(reconcileThreadModelSnapshots([{
			id: 'thread-reconnected',
			model: 'model-from-reconnect',
			updated_at: '2026-07-17T10:00:02.000000',
		}]));

		expect(get(activeModelId)).toBe('model-from-reconnect');
	});

	it('retires the active receipt when the selected model changes', () => {
		activeThreadId.set('thread-visible');
		activeModelId.set('model-before');
		setContextUsage('thread-visible', { total_used: 90 } as never);

		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-visible',
			model: 'model-after',
			updated_at: '2026-07-17T10:00:01.000000',
		}, resetHeartbeat);

		expect(getContextUsage('thread-visible')).toBeNull();
	});

	it('keeps the active receipt when repeated model truth is unchanged', () => {
		activeThreadId.set('thread-visible');
		activeModelId.set('model-current');
		setContextUsage('thread-visible', { total_used: 90 } as never);

		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-visible',
			model: 'model-current',
			updated_at: '2026-07-17T10:00:01.000000',
		}, resetHeartbeat);

		expect(getContextUsage('thread-visible')?.total_used).toBe(90);
	});
});
