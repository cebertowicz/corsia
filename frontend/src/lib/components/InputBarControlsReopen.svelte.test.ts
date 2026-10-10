import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId, threads } from '$lib/stores/threads';
import { activeModelId, models } from '$lib/stores/models';
import { clearAllContextUsage, setContextUsage, contextPreviewByThread } from '$lib/stores/contextUsageByThread';

// The sheet component keeps state across close and reopen, so this needs a
// reactive `sheetView` that a plain mount cannot change (#1953).

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('test-token');
	connectionState.set('connected');
	activeThreadId.set('session-ctx');
	threads.set([{ id: 'session-ctx', title: 'CTX Thread' } as never]);
	activeModelId.set('model-ready');
	models.set([{
		id: 'model-ready', name: 'Model-35B', size: '', backend: 'local', speed: '',
		status: 'ready',
		capabilities: {
			context_window_max: 262144, supports_vision: false, supports_tools: true,
			supports_reasoning: false, reasoning_config: null,
		},
	}]);
	clearAllContextUsage();
	contextPreviewByThread.set(new Map());
	checkpointsByThread.set(new Map());
	threadWorkspaces.set({});
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	connectionState.set('disconnected');
	activeThreadId.set(null);
	threads.set([]);
	activeModelId.set(null);
	models.set([]);
	clearAllContextUsage();
	contextPreviewByThread.set(new Map());
	checkpointsByThread.set(new Map());
	threadWorkspaces.set({});
});

const previewResponse = {
	ok: true,
	json: async () => ({
		preview: {
			model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
			history_tokens: 600, user_tokens: 0, total_used: 600,
			utilization: 0.6, token_source: 'measured', token_usage_available: true,
			snapshot_id: 'preview-1', was_trimmed: false, history_messages: 2,
			context_snapshot: { snapshot_id: 'preview-1', source_inventory: [] },
		},
	}),
};

describe('context sheet reopen (#1953)', () => {
	it('shows the previous preview at once on reopen without reading it again', async () => {
		let previewCalls = 0;
		vi.stubGlobal('fetch', vi.fn(async () => {
			previewCalls += 1;
			return previewResponse;
		}));
		const props = $state<Record<string, unknown>>({
			surface: 'sheets', sheetView: 'context', toggleSheet: () => {},
			isMobile: false, hasImageAttachments: false, inputText: '',
			pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true,
			queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
			onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
		});
		component = mount(InputBarControls, { target: host, props: props as never });
		flushSync();
		await vi.waitFor(() => {
			expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		});

		props.sheetView = null;
		flushSync();
		expect(host.querySelector('.context-sheet')).toBeNull();

		props.sheetView = 'context';
		flushSync();

		// Same thread, model, measured turn and checkpoint: the kept preview is
		// current, so reopening shows it and reads nothing.
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		expect(host.textContent).not.toContain('Loading context');
		expect(host.textContent).not.toContain('Updating…');
		await Promise.resolve();
		expect(previewCalls).toBe(1);
	});

	it('retries a failed read when the sheet is opened again, and only then', async () => {
		let previewCalls = 0;
		vi.stubGlobal('fetch', vi.fn(async () => {
			previewCalls += 1;
			if (previewCalls === 1) return { ok: false, status: 503, json: async () => ({}) };
			return previewResponse;
		}));
		const props = $state<Record<string, unknown>>({
			surface: 'sheets', sheetView: 'context', toggleSheet: () => {},
			isMobile: false, hasImageAttachments: false, inputText: '',
			pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true,
			queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
			onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
		});
		component = mount(InputBarControls, { target: host, props: props as never });
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Context preview failed (503)'));
		expect(previewCalls).toBe(1);

		props.sheetView = null;
		flushSync();
		props.sheetView = 'context';
		flushSync();

		await vi.waitFor(() => {
			expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		});
		expect(previewCalls).toBe(2);
	});

	it('drops the kept preview when a newer turn was measured while the sheet was closed', async () => {
		let previewCalls = 0;
		vi.stubGlobal('fetch', vi.fn(async () => {
			previewCalls += 1;
			if (previewCalls === 1) return previewResponse;
			return new Promise<never>(() => {});
		}));
		const props = $state<Record<string, unknown>>({
			surface: 'sheets', sheetView: 'context', toggleSheet: () => {},
			isMobile: false, hasImageAttachments: false, inputText: '',
			pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true,
			queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
			onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
		});
		component = mount(InputBarControls, { target: host, props: props as never });
		flushSync();
		await vi.waitFor(() => {
			expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		});

		props.sheetView = null;
		flushSync();
		setContextUsage('session-ctx', {
			model_context_window: 1000, system_tokens: 40, tool_def_tokens: 0,
			history_tokens: 700, user_tokens: 0, total_used: 720,
			utilization: 0.72, token_source: 'measured', token_usage_available: true,
			snapshot_id: 'boundary-720', continuity_mode: 'full', was_trimmed: false,
			history_messages: 4, thread_id: 'session-ctx',
		});
		flushSync();

		props.sheetView = 'context';
		flushSync();

		// The older preview must not shadow the newer measured usage, in the
		// sheet or in the chip that reads the shared next-turn value.
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('72%');
		expect(host.querySelector('.ctx-headline')?.textContent).not.toContain('60%');
		await vi.waitFor(() => expect(previewCalls).toBe(2));
	});

	it('does not carry a preview over to another thread', async () => {
		let previewCalls = 0;
		vi.stubGlobal('fetch', vi.fn(async () => {
			previewCalls += 1;
			if (previewCalls === 1) return previewResponse;
			return new Promise<never>(() => {});
		}));
		const props = $state<Record<string, unknown>>({
			surface: 'sheets', sheetView: 'context', toggleSheet: () => {},
			isMobile: false, hasImageAttachments: false, inputText: '',
			pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true,
			queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
			onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
		});
		component = mount(InputBarControls, { target: host, props: props as never });
		flushSync();
		await vi.waitFor(() => {
			expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		});

		activeThreadId.set('another-session');
		flushSync();

		await vi.waitFor(() => expect(previewCalls).toBe(2));
		expect(host.querySelector('.ctx-headline')).toBeNull();
		expect(host.textContent).toContain('Loading context');
	});
});
