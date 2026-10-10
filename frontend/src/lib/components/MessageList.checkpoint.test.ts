import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import MessageList from './MessageList.svelte';
import { token } from '$lib/stores/auth';
import {
	_resetCheckpointsForTests,
	checkpointsByThread,
} from '$lib/stores/checkpoints';
import { activeThreadId, messages } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';

const THREAD_ID = 'thread-checkpoint';

function setCheckpoint(
	status: 'running' | 'captured' | 'installed' | 'failed',
	trigger: 'manual' | 'pressure' | 'model_switch' = 'pressure',
): void {
	checkpointsByThread.set(new Map([[THREAD_ID, {
		checkpoints: [{
			id: 'checkpoint-1', trigger, mode: 'warm', status,
			writerModel: 'omlx@internal/model',
			error: status === 'failed' ? 'provider_raw_failure' : null,
			startedAt: '2026-08-27T12:00:00Z',
		}],
		loading: false,
		error: null,
	}]]));
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function renderList(): void {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('ui-token');
	activeThreadId.set(THREAD_ID);
	messages.set([]);
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	vi.unstubAllGlobals();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	activeThreadId.set(null);
	messages.set([]);
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
});

describe('checkpoint thread status', () => {
	it('shows a background model switch and cancels only its exact id', async () => {
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({
			checkpoint: {
				id: 'checkpoint-1', trigger: 'model_switch', mode: 'warm',
				status: 'cancelled', writerModel: 'omlx@internal/model', error: null,
				startedAt: '2026-08-27T12:00:00Z',
			},
		}), { status: 200, headers: { 'Content-Type': 'application/json' } }));
		vi.stubGlobal('fetch', fetchMock);
		setCheckpoint('running', 'model_switch');
		renderList();

		expect(host.textContent).toContain('Preparing model switch…');
		host.querySelector<HTMLButtonElement>(
			'[data-checkpoint-cancel="checkpoint-1"]',
		)?.click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
			'/api/threads/thread-checkpoint/checkpoints/checkpoint-1/cancel?token=ui-token',
			expect.objectContaining({ method: 'POST' }),
		));
		expect(host.textContent).not.toContain('omlx@internal');
	});

	it('keeps hard-wall input visible in the ordinary pending region', () => {
		setCheckpoint('captured');
		messages.set([{
			id: 'queued-1', thread_id: THREAD_ID, role: 'user',
			content: 'send after checkpoint', created_at: '2026-08-27T12:01:00Z',
			delivery_state: 'queued', delivery_error: null,
		}]);
		renderList();

		expect(host.textContent).toContain('Finishing checkpoint…');
		expect(host.textContent).toContain('send after checkpoint');
		expect(host.textContent).toContain('Sending when ready…');
	});

	it('shows a failed checkpoint without retrying it or exposing raw failure text', () => {
		setCheckpoint('failed');
		renderList();

		expect(host.textContent).toContain('Checkpoint failed');
		expect(host.textContent).not.toContain('provider_raw_failure');
		expect(host.querySelector('[data-checkpoint-cancel]')).toBeNull();
	});

	// The live status is transient by construction, not by a timer: an installed
	// checkpoint is neither active nor failed, so the transcript has nothing to
	// show. Without this, widening `activeCheckpoint` or letting
	// `transcriptCheckpoint` fall back to the latest row unconditionally would
	// strand "Saving context…" in the transcript forever and no test would fail.
	// The completion receipt itself is a separate chip, covered in EventChip.test.
	it('clears the live status once the checkpoint installs', () => {
		setCheckpoint('installed');
		renderList();

		expect(host.querySelector('[data-checkpoint-status]')).toBeNull();
		expect(host.textContent).not.toContain('Saving context');
		expect(host.textContent).not.toContain('Finishing checkpoint');
	});

	it('keeps the status visible while a later checkpoint is still running', () => {
		// Guards the other direction: "installed" must not suppress an active row.
		checkpointsByThread.set(new Map([[THREAD_ID, {
			checkpoints: [
				{
					id: 'checkpoint-2', trigger: 'pressure', mode: 'warm',
					status: 'running', writerModel: 'omlx@internal/model',
					error: null, startedAt: '2026-08-27T12:05:00Z',
				},
				{
					id: 'checkpoint-1', trigger: 'pressure', mode: 'warm',
					status: 'installed', writerModel: 'omlx@internal/model',
					error: null, startedAt: '2026-08-27T12:00:00Z',
				},
			],
			loading: false,
			error: null,
		}]]));
		renderList();

		expect(
			host.querySelector('[data-checkpoint-status]')?.getAttribute('data-checkpoint-status'),
		).toBe('running');
		expect(host.textContent).toContain('Saving context…');
	});
});
