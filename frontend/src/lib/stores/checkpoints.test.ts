import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	_resetCheckpointsForTests,
	activeCheckpoint,
	CheckpointRefusalError,
	cancelCheckpoint,
	checkpointFreshnessLabel,
	checkpointStatusCopy,
	checkpointsByThread,
	loadCheckpoints,
	requestManualCheckpoint,
} from './checkpoints';

const THREAD_ID = 'thread-1';
const TOKEN = 'token';

function response(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'Content-Type': 'application/json' },
	});
}

function row(status: string, id = `checkpoint-${status}`) {
	return {
		id,
		trigger: status === 'running' ? 'model_switch' : 'pressure',
		mode: 'warm',
		status,
		writerModel: 'native/Fortissimo',
		error: status === 'failed' ? 'provider failed' : null,
		startedAt: '2026-08-27T12:00:00Z',
		extraServerField: 'ignored without degrading the checkpoint',
	};
}

afterEach(() => {
	_resetCheckpointsForTests();
	vi.restoreAllMocks();
});

describe('checkpoint history', () => {
	it('loads the six durable states without filtering unknown response fields', async () => {
		const statuses = [
			'running', 'captured', 'installed', 'overtaken', 'failed', 'cancelled',
		];
		vi.stubGlobal('fetch', vi.fn(async () => response({
			checkpoints: statuses.map(status => row(status)),
			unknownEnvelopeField: true,
		})));

		const loaded = await loadCheckpoints(THREAD_ID, TOKEN);

		expect(loaded.map(checkpoint => checkpoint.status)).toEqual(statuses);
		expect(get(checkpointsByThread).get(THREAD_ID)?.checkpoints).toEqual(loaded);
		expect(activeCheckpoint(get(checkpointsByThread).get(THREAD_ID))?.id)
			.toBe('checkpoint-running');
	});

	it.each([
		['running', 'Preparing model switch…'],
		['captured', 'Finishing checkpoint…'],
		['installed', 'Context saved'],
		['overtaken', 'Checkpoint saved to history'],
		['failed', 'Checkpoint failed'],
		['cancelled', 'Checkpoint cancelled'],
	] as const)('gives %s one plain status sentence', (status, copy) => {
		expect(checkpointStatusCopy(row(status) as never)).toBe(copy);
	});
});

describe('checkpoint actions', () => {
	it('starts a manual checkpoint through the clean endpoint', async () => {
		const fetchMock = vi.fn(async () => response({ checkpoint: row('running') }, 202));
		vi.stubGlobal('fetch', fetchMock);

		const checkpoint = await requestManualCheckpoint(THREAD_ID, TOKEN);

		expect(checkpoint.id).toBe('checkpoint-running');
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/threads/thread-1/checkpoints?token=token',
			expect.objectContaining({ method: 'POST' }),
		);
		expect(get(checkpointsByThread).get(THREAD_ID)?.checkpoints[0]).toEqual(checkpoint);
	});

	it('surfaces the backend\'s exact refusal reason instead of a generic retry (#1952)', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(
			{ detail: { message: 'checkpoint already covers the sealed history', reason: 'no_new_history' } },
			409,
		)));

		await expect(requestManualCheckpoint(THREAD_ID, TOKEN)).rejects.toMatchObject({
			message: 'Nothing new since the last checkpoint.',
			reason: 'no_new_history',
		});
	});

	it('falls back to the generic message for an unrecognized or missing detail', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response({}, 409)));

		const error = await requestManualCheckpoint(THREAD_ID, TOKEN).catch((exc) => exc);

		expect(error).toBeInstanceOf(CheckpointRefusalError);
		expect(error.message).toBe('Could not start a checkpoint. Try again.');
		expect(error.reason).toBe('unknown');
	});

	it('cancels only the checkpoint named in the route', async () => {
		const fetchMock = vi.fn(async () => response({
			checkpoint: row('cancelled', 'checkpoint-exact'),
		}));
		vi.stubGlobal('fetch', fetchMock);

		const checkpoint = await cancelCheckpoint(
			THREAD_ID,
			'checkpoint-exact',
			TOKEN,
		);

		expect(checkpoint.status).toBe('cancelled');
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/threads/thread-1/checkpoints/checkpoint-exact/cancel?token=token',
			expect.objectContaining({ method: 'POST' }),
		);
	});
});

describe('checkpointFreshnessLabel', () => {
	const startedAt = '2026-08-27T12:00:00Z';
	const started = new Date(startedAt).getTime();

	it.each([
		[0, 'Checkpointed just now'],
		[1, 'Checkpointed 1 minute ago'],
		[45, 'Checkpointed 45 minutes ago'],
		[60, 'Checkpointed 1 hour ago'],
		[180, 'Checkpointed 3 hours ago'],
	])('labels %s minutes since the checkpoint as %s', (minutesElapsed, label) => {
		const now = started + minutesElapsed * 60_000;
		expect(checkpointFreshnessLabel(row('installed', 'checkpoint-1') as never, now)).toBe(label);
	});
});
