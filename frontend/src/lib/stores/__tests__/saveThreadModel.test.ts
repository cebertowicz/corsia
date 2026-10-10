import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import {
	_resetThreadModelTruthForTests,
	activeModelId,
	saveThreadModel,
} from '$lib/stores/models';
import { activeThreadId, threads } from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';

function seed(model: string, updatedAt = '2026-07-17T10:00:00.000000') {
	threads.set([{ id: 'thread-1', model, updated_at: updatedAt } as never]);
}

function currentModel(): string | null | undefined {
	return get(threads).find((thread) => thread.id === 'thread-1')?.model;
}

function modelResponse(
	model: string,
	updatedAt: string,
	warning?: string,
): Response {
	return new Response(
		JSON.stringify({
			model,
			model_updated_at: updatedAt,
			...(warning ? { warning } : {}),
		}),
		{ status: 200, headers: { 'Content-Type': 'application/json' } },
	);
}

describe('saveThreadModel', () => {
	beforeEach(() => {
		_resetThreadModelTruthForTests();
		activeThreadId.set(null);
		activeModelId.set(null);
		threads.set([]);
	});

	afterEach(() => vi.restoreAllMocks());

	it('updates the session only after the PATCH succeeds', async () => {
		seed('model-before');
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		vi.stubGlobal('fetch', vi.fn(() => patch));

		const saving = saveThreadModel('thread-1', 'model-after', 'tok');
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
		expect(currentModel()).toBe('model-before');

		resolvePatch(modelResponse('model-after', '2026-07-17T10:00:01.000000'));
		await saving;

		expect(currentModel()).toBe('model-after');
	});

	it('returns a pending receipt without changing the selected model', async () => {
		seed('model-before');
		vi.stubGlobal('fetch', vi.fn(async () => new Response(
			JSON.stringify({
				operation_id: 'operation-1',
				status: 'pending_checkpoint',
				pending_model: 'model-after',
				checkpoint_required: true,
			}),
			{ status: 202, headers: { 'Content-Type': 'application/json' } },
		)));

		await expect(saveThreadModel('thread-1', 'model-after', 'tok'))
			.resolves.toEqual({
			kind: 'pending',
			operationId: 'operation-1',
			status: 'pending_checkpoint',
			pendingModel: 'model-after',
			checkpointRequired: true,
		});
		expect(fetch).toHaveBeenCalledWith(
			'/api/threads/thread-1?token=tok',
			expect.objectContaining({
				// No checkpoint choice rides along: the server decides, and it
				// decides the warm path.
				body: JSON.stringify({ model: 'model-after' }),
			}),
		);
		expect(currentModel()).toBe('model-before');
	});

	it('does not overwrite newer cross-tab truth when an older PATCH resolves late', async () => {
		seed('model-before');
		activeThreadId.set('thread-1');
		activeModelId.set('model-before');
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		vi.stubGlobal('fetch', vi.fn(() => patch));

		const saving = saveThreadModel('thread-1', 'model-after', 'tok');
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-1',
			model: 'newer-model-truth',
			updated_at: '2026-07-17T10:00:02.000000',
		}, () => {});

		resolvePatch(modelResponse(
			'model-after',
			'2026-07-17T10:00:01.000000',
		));
		await saving;

		expect(currentModel()).toBe('newer-model-truth');
		expect(get(activeModelId)).toBe('newer-model-truth');
	});

	it('surfaces the server detail and preserves the session when the PATCH is rejected', async () => {
		seed('model-before');
		vi.stubGlobal('fetch', vi.fn(async () => new Response(
			JSON.stringify({ detail: 'Destination context does not fit' }),
			{ status: 409, headers: { 'Content-Type': 'application/json' } },
		)));

		await expect(saveThreadModel('thread-1', 'model-after', 'tok'))
			.rejects.toThrow('Destination context does not fit');
		expect(currentModel()).toBe('model-before');
	});

	it('propagates network failures without changing the session', async () => {
		seed('model-before');
		vi.stubGlobal('fetch', vi.fn(async () => {
			throw new Error('network down');
		}));

		await expect(saveThreadModel('thread-1', 'model-after', 'tok'))
			.rejects.toThrow('network down');
		expect(currentModel()).toBe('model-before');
	});

	it('returns a committed post-switch warning without rolling back the session', async () => {
		seed('model-before');
		vi.stubGlobal('fetch', vi.fn(async () => modelResponse(
			'model-after',
			'2026-07-17T10:00:01.000000',
			'Model changed, but cleanup must be retried',
		)));

		await expect(saveThreadModel('thread-1', 'model-after', 'tok'))
			.resolves.toMatchObject({
				kind: 'committed',
				accepted: true,
				warning: 'Model changed, but cleanup must be retried',
			});
		expect(currentModel()).toBe('model-after');
	});
});
