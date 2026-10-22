import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { _resetCheckpointsForTests } from '$lib/stores/checkpoints';
import { clearAllContextUsage } from '$lib/stores/contextUsageByThread';
import { _resetThreadModelTruthForTests, acceptThreadModelTruth, activeModelId, finishThreadModelSwitch, models, threadModelFor } from '$lib/stores/models';
import { threadCatalogs } from '$lib/stores/modelsState';
import { subagentMeta, subagentThreadRows } from '$lib/stores/subagents';
import { activeThreadId, reasoningEffort, threads } from '$lib/stores/threads';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

// The picker's Local group status reads model sources; these tests count only thread requests.
vi.mock('$lib/stores/localPickerStatus', async () => {
	const { readable } = await import('svelte/store');
	return { localStatus: readable(null), canAddLocalModel: readable(false), canSetUpLocalModel: readable(false), retryLocalSetup: vi.fn(), watchLocalStatus: () => () => {} };
});

// A worker's composer beside the open thread's: the worker's model and effort controls
// read and change the worker, never the open thread.
const PARENT_MODEL = 'omlx@ns-test/Parent-9B';
const WORKER_MODEL = 'omlx@ns-test/Worker-9B';
const NEXT_MODEL = 'omlx@ns-test/Next-9B';

const reasoning = { type: 'boolean' as const, default: true };

function model(id: string, name: string) {
	return {
		id, name, size: '', backend: 'network' as const, provider: 'omlx', speed: '', status: 'ready' as const,
		litellm_model: id,
		capabilities: {
			context_window_max: 262144, supports_vision: false, supports_tools: true,
			supports_reasoning: true, reasoning_config: reasoning,
		},
	};
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const originalFetch = globalThis.fetch;

function mountControls(props: Record<string, unknown>) {
	component = mount(InputBarControls, {
		target: host,
		props: {
			threadId: 'worker', surface: 'row', sheetView: null, toggleSheet: () => {}, isMobile: false,
			hasImageAttachments: false, inputText: '', pendingAttachmentCount: 0, hasMessageToSend: false,
			canSendToModel: true, queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
			onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
			...props,
		},
	});
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	connectionState.set('connected');
	finishThreadModelSwitch('worker');
	_resetThreadModelTruthForTests();
	acceptThreadModelTruth('worker', WORKER_MODEL, '2026-10-20T09:59:00.000000');
	_resetCheckpointsForTests();
	clearAllContextUsage();
	activeThreadId.set('parent');
	activeModelId.set(PARENT_MODEL);
	reasoningEffort.set('medium');
	threads.set([{ id: 'parent', model: PARENT_MODEL, reasoning_effort: 'medium' } as never]);
	subagentMeta.set(new Map([['worker', {
		childThreadId: 'worker', parentThreadId: 'parent', name: 'Worker bee', provider: 'cc', status: 'running',
	}]]));
	subagentThreadRows.set(new Map([['worker', { reasoning_effort: 'none' }]]));
	const catalog = [model(PARENT_MODEL, 'Parent-9B'), model(WORKER_MODEL, 'Worker-9B'), model(NEXT_MODEL, 'Next-9B')];
	models.set(catalog);
	threadCatalogs.set(new Map([['worker', catalog]]));
	globalThis.fetch = vi.fn(async () => new Response(
		JSON.stringify({ model: NEXT_MODEL, model_updated_at: '2026-10-20T10:00:01.000000' }),
		{ status: 200, headers: { 'Content-Type': 'application/json' } },
	)) as unknown as typeof fetch;
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.fetch = originalFetch;
	token.set(null);
	connectionState.set('disconnected');
	activeThreadId.set(null);
	activeModelId.set(null);
	reasoningEffort.set(null);
	threads.set([]);
	subagentMeta.set(new Map());
	subagentThreadRows.set(new Map());
	models.set([]);
	threadCatalogs.set(new Map());
	clearAllContextUsage();
});

describe('worker composer reads and changes its own thread', () => {
	it("shows the model chip and effort chip with the worker's own selection", () => {
		mountControls({});

		expect(host.querySelector('button.model-chip .model-chip-name')?.textContent?.trim()).toBe('Worker-9B');
		expect(host.querySelector('button.effort-chip')?.textContent?.trim()).toBe('OFF');
	});

	it("reads the worker's own catalog, not the open thread's, for its model and send state", () => {
		// On the parent's account the worker's model is blocked; on the worker's own it is ready.
		models.set([model(PARENT_MODEL, 'Parent-9B'), { ...model(WORKER_MODEL, 'Worker-9B'), status: 'unavailable' as never }]);
		mountControls({});
		expect(host.querySelector('button.model-chip .model-chip-name')?.textContent?.trim()).toBe('Worker-9B');

		unmount(component!);
		component = null;
		host.replaceChildren();
		threadCatalogs.set(new Map([['worker', [{ ...model(WORKER_MODEL, 'Worker-9B'), status: 'unavailable' as never }]]]));
		mountControls({});
		expect(host.querySelector('button.model-chip .model-chip-name')?.textContent?.trim()).toBe('Unavailable · Worker-9B');
	});

	it("reads the worker's catalog when its model sheet opens", async () => {
		mountControls({});
		host.querySelector<HTMLButtonElement>('button.model-chip')!.click();
		flushSync();

		await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
		const [url] = vi.mocked(globalThis.fetch).mock.calls[0] as [string, RequestInit];
		expect(url).toContain('thread_id=worker');
	});

	it("shows the open thread's selection on the open thread's composer", () => {
		mountControls({ threadId: 'parent' });

		expect(host.querySelector('button.model-chip .model-chip-name')?.textContent?.trim()).toBe('Parent-9B');
		expect(host.querySelector('button.effort-chip')?.textContent?.trim()).toBe('ON');
	});

	it('asks to select a model while the worker has none, not the open thread\'s', () => {
		_resetThreadModelTruthForTests();
		mountControls({});

		expect(host.querySelector('button.model-chip .model-chip-name')?.textContent?.trim()).toBe('Select Model');
	});

	it("choosing a model switches the worker's thread, not the open thread's", async () => {
		mountControls({ surface: 'sheets', sheetView: 'model' });

		const rows = [...host.querySelectorAll<HTMLButtonElement>('button.model-list-item')];
		const next = rows.find(row => row.textContent?.includes('Next-9B'));
		expect(next).toBeDefined();
		next!.click();

		await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
		const [url, init] = vi.mocked(globalThis.fetch).mock.calls[0] as [string, RequestInit];
		expect(url).toContain('/api/threads/worker');
		expect(url).not.toContain('/api/threads/parent');
		expect(init.method).toBe('PATCH');
		expect(init.body).toBe(JSON.stringify({ model: NEXT_MODEL }));
		await vi.waitFor(() => expect(threadModelFor('worker')).toBe(NEXT_MODEL));
		expect(get(activeModelId)).toBe(PARENT_MODEL);
	});

	it("hides the Local only switch in a worker's model sheet, which belongs to the open thread's chat", () => {
		mountControls({ surface: 'sheets', sheetView: 'model' });
		expect(host.querySelector('.local-only-setting')).toBeNull();

		unmount(component!);
		component = null;
		host.replaceChildren();
		mountControls({ threadId: 'parent', surface: 'sheets', sheetView: 'model' });
		expect(host.querySelector('.local-only-setting')).not.toBeNull();
	});

	it("a worker's effort change saves to the worker and leaves the open thread's effort alone", async () => {
		reasoningEffort.set('high');
		mountControls({ surface: 'sheets', sheetView: 'thinking' });

		const on = [...host.querySelectorAll<HTMLButtonElement>('button.seg-btn')]
			.find(button => button.textContent?.trim() === 'On');
		expect(on).toBeDefined();
		on!.click();

		await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
		const [url, init] = vi.mocked(globalThis.fetch).mock.calls[0] as [string, RequestInit];
		expect(url).toContain('/api/threads/worker');
		expect(init.body).toBe(JSON.stringify({ reasoning_effort: 'medium' }));
		expect(get(subagentThreadRows).get('worker')?.reasoning_effort).toBe('medium');
		expect(get(reasoningEffort)).toBe('high');
	});
});
