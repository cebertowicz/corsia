import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import InputBarModelSheet from './InputBarModelSheet.svelte';
import { emitProductEvent } from '$lib/product_events';
import { token } from '$lib/stores/auth';
import {
	_resetCheckpointsForTests,
	checkpointsByThread,
} from '$lib/stores/checkpoints';
import {
	_resetThreadModelTruthForTests,
	activeModelId,
	finishThreadModelSwitch,
	models,
} from '$lib/stores/models';
import { activeThreadId, reasoningEffort, threads } from '$lib/stores/threads';
import { toasts } from '$lib/stores/toast';
import { handleMessage } from '$lib/ws/handlers';
import { clearAllContextUsage } from '$lib/stores/contextUsageByThread';

vi.mock('$lib/product_events', () => ({
	emitProductEvent: vi.fn(),
}));

// The picker's Local group status reads model sources; these tests count only model-switch requests.
vi.mock('$lib/stores/localPickerStatus', async () => {
	const { readable } = await import('svelte/store');
	return { localStatus: readable(null), canAddLocalModel: readable(false), canSetUpLocalModel: readable(false), neverLocalHost: readable(false), retryLocalSetup: vi.fn(), watchLocalStatus: () => () => {} };
});

const DISPATCH_ID = 'omlx@ns-test/Model-9B-mlx-mxfp8';
const CATALOG_ID = 'vmlx-cbe7d13e';
const SWITCHED_AT = '2026-07-17T10:00:01.000000';

let host: HTMLElement;
let component: Record<string, any> | null = null;
const originalFetch = globalThis.fetch;

function seedModel() {
	models.set([{
		id: CATALOG_ID,
		name: 'Model-9B-mlx-mxfp8',
		desc: 'MLX',
		size: '',
		backend: 'network',
		provider: 'omlx',
		speed: '',
		status: 'ready',
		litellm_model: DISPATCH_ID,
		capabilities: {
			context_window_max: 262144,
			supports_vision: true,
			supports_tools: true,
			supports_reasoning: true,
			reasoning_config: { type: 'boolean', default: true },
		},
		server_name: 'oMLX',
	}]);
}

function modelResponse(
	model = DISPATCH_ID,
	updatedAt = SWITCHED_AT,
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


function checkpointsResponse(): Response {
	return new Response(JSON.stringify({
		checkpoints: [{
			id: 'checkpoint-1', trigger: 'model_switch', mode: 'warm',
			status: 'running', writerModel: 'previous-model', error: null,
			startedAt: SWITCHED_AT,
		}],
	}), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function mountControls() {
	return mount(InputBarControls, {
		target: host,
		props: {
			surface: 'row',
			sheetView: null,
			toggleSheet: () => {},
			isMobile: false,
			hasImageAttachments: false,
			inputText: '',
			pendingAttachmentCount: 0,
			hasMessageToSend: false,
			canSendToModel: true,
			queueUntilSwitch: false,
			onQueueAfterTurn: () => {},
			onAttachFile: () => {},
			onAttachCamera: () => {},
			onAttachPhotos: () => {},
			onSendClick: () => {},
		},
	});
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	finishThreadModelSwitch('thread-1');
	_resetThreadModelTruthForTests();
	_resetCheckpointsForTests();
	activeThreadId.set('thread-1');
	activeModelId.set(DISPATCH_ID);
	threads.set([{
		id: 'thread-1',
		model: DISPATCH_ID,
		updated_at: '2026-07-17T10:00:00.000000',
	} as never]);
	clearAllContextUsage();
	reasoningEffort.set(null);
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	vi.mocked(emitProductEvent).mockClear();
	seedModel();
	globalThis.fetch = vi.fn(async () => ({
		ok: true,
		json: async () => ({}),
	})) as unknown as typeof fetch;
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	globalThis.fetch = originalFetch;
});

describe('composer model identity', () => {
	it('renders the selected model when activeModelId is the persisted dispatch string', () => {
		component = mountControls();
		flushSync();

		expect(host.textContent).toContain('Model-9B-mlx-mxfp8');
		expect(host.textContent).not.toContain('Select Model');
	});

	it('keeps a saved selector visible when discovery no longer returns it', () => {
		activeModelId.set('codex/gpt-5-codex');
		models.set([]);
		component = mountControls();
		flushSync();

		expect(host.textContent).toContain('Unavailable · codex/gpt-5-codex');
		expect(host.textContent).not.toContain('Select Model');
	});

	it('persists the dispatch string when selecting a catalog-backed model', async () => {
		activeModelId.set('previous-model');
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		globalThis.fetch = vi.fn(() => patch) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		const button = host.querySelector('button.model-list-item') as HTMLButtonElement;
		button.click();
		await Promise.resolve();

		expect(get(activeModelId)).toBe('previous-model');
		expect(emitProductEvent).not.toHaveBeenCalled();

		resolvePatch(modelResponse());
		await vi.waitFor(() => expect(get(activeModelId)).toBe(DISPATCH_ID));

		expect(globalThis.fetch).toHaveBeenCalledWith(
			'/api/threads/thread-1?token=tok',
			expect.objectContaining({
				method: 'PATCH',
				body: JSON.stringify({ model: DISPATCH_ID }),
			}),
		);
		expect(emitProductEvent).toHaveBeenCalledWith(
			'model',
			'switched',
			expect.objectContaining({ model_id: DISPATCH_ID }),
		);
	});

	it('keeps the prior selection and shows the server error when persistence fails', async () => {
		activeModelId.set('previous-model');
		globalThis.fetch = vi.fn(async () => new Response(
			JSON.stringify({ detail: 'Model switch blocked' }),
			{ status: 409, headers: { 'Content-Type': 'application/json' } },
		)) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();

		await vi.waitFor(() => {
			expect(get(toasts).at(-1)).toMatchObject({
				message: 'Model switch blocked',
				type: 'error',
			});
		});
		expect(get(activeModelId)).toBe('previous-model');
		expect(emitProductEvent).not.toHaveBeenCalled();
	});

	it('sends one unattended switch and follows the fold it starts', async () => {
		// A switch that needs a checkpoint used to bounce back as a dialog
		// asking WHICH model should fold. The server now folds on the model
		// being left and carries on, so the pick is one action: one PATCH
		// carrying the model alone, and a pending receipt to follow.
		activeModelId.set('previous-model');
		threads.update(rows => rows.map(row => ({ ...row, model: 'previous-model' })));
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(new Response(JSON.stringify({
				operation_id: 'operation-1', status: 'running',
				pending_model: DISPATCH_ID, checkpoint_required: true,
			}), { status: 202, headers: { 'Content-Type': 'application/json' } }))
			.mockResolvedValueOnce(checkpointsResponse());
		globalThis.fetch = fetchMock as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();

		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
		expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
			body: JSON.stringify({ model: DISPATCH_ID }),
		}));
		expect(host.textContent).not.toContain('Checkpoint required');
		// The switch is not applied locally until the fold lands.
		expect(get(activeModelId)).toBe('previous-model');
		expect(get(threads)[0]?.model).toBe('previous-model');
		expect(get(checkpointsByThread).get('thread-1')?.checkpoints[0]?.status)
			.toBe('running');
		expect(emitProductEvent).not.toHaveBeenCalled();
	});

	it('applies a committed switch and surfaces its post-effect warning', async () => {
		activeModelId.set('previous-model');
		globalThis.fetch = vi.fn(async () => modelResponse(
			DISPATCH_ID,
			SWITCHED_AT,
			'Model changed, but cleanup must be retried',
		)) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();

		await vi.waitFor(() => {
			expect(get(activeModelId)).toBe(DISPATCH_ID);
			expect(get(toasts).at(-1)).toMatchObject({
				message: 'Model changed, but cleanup must be retried',
				type: 'info',
			});
		});
		expect(emitProductEvent).toHaveBeenCalledOnce();
	});

	it('applies a committed switch when the active thread is hidden from the session list', async () => {
		activeModelId.set('previous-model');
		threads.set([]);
		globalThis.fetch = vi.fn(async () => modelResponse()) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();

		await vi.waitFor(() => expect(get(activeModelId)).toBe(DISPATCH_ID));
		expect(emitProductEvent).toHaveBeenCalledOnce();
	});

	it('does not apply a completed save after the user switches threads', async () => {
		activeModelId.set('previous-model');
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		globalThis.fetch = vi.fn(() => patch) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();
		await Promise.resolve();
		activeThreadId.set('thread-2');
		activeModelId.set('thread-2-model');
		resolvePatch(modelResponse());
		await Promise.resolve();
		await Promise.resolve();

		expect(get(activeModelId)).toBe('thread-2-model');
		expect(emitProductEvent).not.toHaveBeenCalled();
	});

	it('allows only one model switch at a time for the active thread', async () => {
		activeModelId.set('previous-model');
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		globalThis.fetch = vi.fn(() => patch) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		const button = host.querySelector('button.model-list-item') as HTMLButtonElement;
		button.click();
		button.click();
		await Promise.resolve();

		expect(globalThis.fetch).toHaveBeenCalledOnce();

		resolvePatch(modelResponse());
		await vi.waitFor(() => expect(get(activeModelId)).toBe(DISPATCH_ID));
	});

	it('keeps the thread switch locked when the model sheet remounts', async () => {
		activeModelId.set('previous-model');
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		globalThis.fetch = vi.fn(() => patch) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();
		await Promise.resolve();
		unmount(component);
		component = null;
		host.replaceChildren();
		component = mount(InputBarModelSheet, { target: host });
		flushSync();
		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();
		await Promise.resolve();

		expect(globalThis.fetch).toHaveBeenCalledOnce();
		resolvePatch(modelResponse());
		await vi.waitFor(() => expect(get(activeModelId)).toBe(DISPATCH_ID));
	});

	it('does not let an older save overwrite newer truth for the same thread', async () => {
		activeModelId.set('previous-model');
		threads.set([{
			id: 'thread-1',
			model: 'previous-model',
			updated_at: '2026-07-17T10:00:00.000000',
		} as never]);
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		globalThis.fetch = vi.fn(() => patch) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();
		await Promise.resolve();
		handleMessage({
			type: 'session_updated',
			thread_id: 'thread-1',
			model: 'newer-model-truth',
			updated_at: '2026-07-17T10:00:02.000000',
		}, () => {});
		resolvePatch(modelResponse(
			DISPATCH_ID,
			SWITCHED_AT,
			'Older switch committed before newer truth',
		));

		await vi.waitFor(() => expect(get(toasts).at(-1)).toMatchObject({
			message: 'Older switch committed before newer truth',
			type: 'info',
		}));
		expect(get(activeModelId)).toBe('newer-model-truth');
		expect(get(threads)[0]?.model).toBe('newer-model-truth');
		expect(emitProductEvent).not.toHaveBeenCalled();
	});

	it('surfaces a committed warning after the user switches threads', async () => {
		activeModelId.set('previous-model');
		let resolvePatch!: (response: Response) => void;
		const patch = new Promise<Response>((resolve) => {
			resolvePatch = resolve;
		});
		globalThis.fetch = vi.fn(() => patch) as unknown as typeof fetch;
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();
		await Promise.resolve();
		activeThreadId.set('thread-2');
		activeModelId.set('thread-2-model');
		resolvePatch(modelResponse(
			DISPATCH_ID,
			SWITCHED_AT,
			'Model changed, but cleanup must be retried',
		));

		await vi.waitFor(() => expect(get(toasts).at(-1)).toMatchObject({
			message: 'Model changed, but cleanup must be retried',
			type: 'info',
		}));
		expect(get(activeModelId)).toBe('thread-2-model');
	});

	it('does not PATCH or emit a switch event for the already-selected model', async () => {
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		(host.querySelector('button.model-list-item') as HTMLButtonElement).click();
		await Promise.resolve();

		expect(globalThis.fetch).not.toHaveBeenCalled();
		expect(emitProductEvent).not.toHaveBeenCalled();
	});

	it('submits a newer model choice while another switch is pending', async () => {
		activeModelId.set('previous-model');
		threads.update(rows => rows.map(row => ({
			...row,
			pending_model: 'codex/other',
			switch_status: 'preparing',
			switch_operation_id: 'operation-1',
		})));
		globalThis.fetch = vi.fn(async () => modelResponse(DISPATCH_ID));
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		const button = host.querySelector<HTMLButtonElement>('button.model-list-item')!;
		expect(button.disabled).toBe(false);
		button.click();
		await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledOnce());

		expect(globalThis.fetch).toHaveBeenCalledOnce();
	});


	it('keeps the composer ready-only and marks retained cache rows neutrally', () => {
		models.set([
			{
				id: 'codex/gpt-5.6-sol', name: 'Codex · Sol', size: '',
				backend: 'codex', provider: 'codex', speed: '', status: 'ready', available: true,
				cache_marker: 'Cached',
			},
			{
				id: 'codex/gpt-5.6-terra', name: 'Codex · Terra', size: '',
				backend: 'codex', provider: 'codex', speed: '', status: 'unavailable',
			},
		]);
		component = mount(InputBarModelSheet, { target: host });
		flushSync();

		expect(host.textContent).toContain('Codex · Sol');
		expect(host.textContent).toContain('Cached');
		expect(host.textContent).not.toContain('Codex · Terra');
	});
});
