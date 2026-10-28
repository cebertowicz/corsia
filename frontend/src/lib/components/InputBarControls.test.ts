import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { applyLifecycle, applyTerminal } from '$lib/ws/turnState';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId, threads, type ContextUsage } from '$lib/stores/threads';
import { contextSheetUsageSummary } from './contextSheetModel';
import { activeModelId, models } from '$lib/stores/models';
import { clearAllContextUsage, setContextUsage, contextPreviewByThread } from '$lib/stores/contextUsageByThread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('test-token');
	connectionState.set('connected');
	activeThreadId.set('session-ctx');
	threads.set([{ id: 'session-ctx', title: 'CTX Thread' } as never]);
	activeModelId.set('model-ready');
	models.set([{
		id: 'model-ready',
		name: 'Model-35B',
		size: '',
		backend: 'local',
		speed: '',
		status: 'ready',
		capabilities: {
			context_window_max: 262144,
			supports_vision: false,
			supports_tools: true,
			supports_reasoning: false,
			reasoning_config: null,
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

function mountContextControls(hasImageAttachments = false) {
	component = mount(InputBarControls, {
		target: host,
		props: {
			surface: 'sheets',
			sheetView: 'context',
			toggleSheet: () => {},
			isMobile: false,
			hasImageAttachments,
			inputText: '',
			pendingAttachmentCount: 0,
			hasMessageToSend: false,
			canSendToModel: true,
			queueUntilSwitch: false,
			onAttachFile: () => {},
			onAttachCamera: () => {},
			onAttachPhotos: () => {},
			onSendClick: () => {}, onQueueAfterTurn: () => {},
		},
	});
	flushSync();
}

function mountContextRow(canSendToModel = true, queueUntilSwitch = false) {
	component = mount(InputBarControls, {
		target: host,
		props: {
			surface: 'row', sheetView: null, toggleSheet: vi.fn(),
			isMobile: false, hasImageAttachments: false, inputText: '',
			pendingAttachmentCount: 0, hasMessageToSend: false,
			canSendToModel, queueUntilSwitch,
			onAttachFile: () => {}, onAttachCamera: () => {}, onAttachPhotos: () => {},
			onSendClick: () => {}, onQueueAfterTurn: () => {},
		},
	});
	flushSync();
}

it('shows Send for a comment-only draft during an active turn', () => {
	const identity = { turnId: 'turn-comment', turnOrder: 1 };
	applyLifecycle('session-ctx', identity);
	const onSendClick = vi.fn();
	component = mount(InputBarControls, {
		target: host,
		props: {
			surface: 'row', threadId: 'session-ctx', sheetView: null, toggleSheet: vi.fn(),
			isMobile: false, hasImageAttachments: false, inputText: '',
			pendingAttachmentCount: 0, hasMessageToSend: true,
			canSendToModel: true, queueUntilSwitch: false,
			onAttachFile: () => {}, onAttachCamera: () => {}, onAttachPhotos: () => {},
			onSendClick, onQueueAfterTurn: () => {},
		},
	});
	flushSync();
	const button = host.querySelector<HTMLButtonElement>('.send-btn')!;
	expect(button.disabled).toBe(false);
	expect(button.getAttribute('aria-label')).toBe('Send message');
	expect(button.classList.contains('stop-btn')).toBe(false);
	button.click();
	expect(onSendClick).toHaveBeenCalledOnce();
	applyTerminal('session-ctx', identity);
});

it('warns when an image attachment is paired with a non-vision model', () => {
	mountContextControls(true);

	expect(host.textContent).toContain("doesn't support images");
});

function contextPreviewResponse(
	total: number,
	tokenSource: 'measured' | 'estimated',
	snapshotId: string,
) {
	return {
		ok: true,
		json: async () => ({
			preview: {
				model_context_window: 1000,
				system_tokens: 0,
				tool_def_tokens: 0,
				history_tokens: total,
				user_tokens: 0,
				total_used: total,
				utilization: total / 1000,
				token_source: tokenSource,
				token_usage_available: tokenSource === 'measured',
				snapshot_id: snapshotId,
				was_trimmed: false,
				history_messages: 2,
				context_snapshot: { snapshot_id: snapshotId, source_inventory: [] },
			},
		}),
	};
}

function measuredContextReceipt(
	total: number,
	continuityMode: 'full' | 'checkpoint_tail' = 'full',
) {
	return {
		model_context_window: 1000,
		system_tokens: 40,
		tool_def_tokens: 0,
		history_tokens: total - 60,
		user_tokens: 0,
		total_used: total,
		utilization: total / 1000,
		token_source: 'measured' as const,
		token_usage_available: true,
		snapshot_id: `boundary-${total}`,
		continuity_mode: continuityMode,
		was_trimmed: false,
		history_messages: 2,
		thread_id: 'session-ctx',
		catalog_model_id: 'model-ready',
	};
}

function previewResponse() {
	return {
		ok: true,
		json: async () => ({
			preview: {
				model_context_window: 1000,
				system_tokens: 0,
				tool_def_tokens: 0,
				history_tokens: 100,
				user_tokens: 0,
				total_used: 100,
				utilization: 0.1,
				token_source: 'measured',
				was_trimmed: false,
				history_messages: 2,
				context_snapshot: { source_inventory: [] },
			},
		}),
	};
}

function setCheckpoint(
	status: 'running' | 'captured' | 'installed' | 'overtaken' | 'failed' | 'cancelled',
	trigger: 'manual' | 'pressure' | 'model_switch' = 'model_switch',
) {
	checkpointsByThread.set(new Map([['session-ctx', {
		checkpoints: [{
			id: 'checkpoint-1', trigger, mode: 'warm', status,
			writerModel: 'model-ready', error: status === 'failed' ? 'failed' : null,
			startedAt: '2026-07-21T08:00:00Z',
		}],
		loading: false,
		error: null,
	}]]));
}

describe('InputBarControls context sources', () => {
	it('keeps Send disabled when the selected model is unavailable without a live switch target', () => {
		activeModelId.set('model-unavailable');
		models.set([{
			id: 'model-unavailable', name: 'Unavailable model', size: '', backend: 'local',
			speed: '', status: 'unavailable', capabilities: undefined,
		} as never]);
		mountContextRow(false);

		const send = host.querySelector<HTMLButtonElement>('button.send-btn');
		expect(send?.disabled).toBe(true);
		expect(send?.title).toBe('Pick a loaded model to send');
	});

	it('labels the model chip with the canonical name, falling back to the row name', () => {
		const caps = { context_window_max: 262144, supports_vision: false, supports_tools: true, supports_reasoning: false, reasoning_config: null };
		activeModelId.set('claude-code/sonnet');
		models.set([{
			id: 'cc/sonnet', litellm_model: 'claude-code/sonnet', name: 'Claude Code · Sonnet 5.5', display_name: 'Sonnet 5.5',
			size: '', backend: 'cc', speed: '', status: 'ready', capabilities: caps,
		}]);
		mountContextRow(false);
		const chip = () => host.querySelector('button.model-chip .model-chip-name')?.textContent?.trim();
		expect(chip()).toBe('Sonnet 5.5');
		models.set([{
			id: 'claude-code/sonnet', name: 'Plain Name', size: '', backend: 'local', speed: '', status: 'ready', capabilities: caps,
		}]);
		flushSync();
		expect(chip()).toBe('Plain Name');
	});

	it.each(['running', 'captured'] as const)(
		'keeps Send enabled while an available target owns the confirmed %s switch',
		(status) => {
			activeModelId.set('model-unavailable');
			models.set([
				{
					id: 'model-unavailable', name: 'Unavailable source', size: '', backend: 'local',
					speed: '', status: 'unavailable', capabilities: undefined,
				},
				{
					id: 'model-target', name: 'Available target', size: '', backend: 'local',
					speed: '', status: 'ready', capabilities: undefined,
				},
			] as never);
			setCheckpoint(status);
			component = mount(InputBarControls, {
				target: host,
				props: {
					surface: 'row', sheetView: null, toggleSheet: vi.fn(),
					isMobile: false, hasImageAttachments: false, inputText: 'queue this thought',
					pendingAttachmentCount: 0, hasMessageToSend: true,
					canSendToModel: true, queueUntilSwitch: true,
					onAttachFile: () => {}, onAttachCamera: () => {}, onAttachPhotos: () => {},
					onSendClick: () => {}, onQueueAfterTurn: () => {},
				},
			});
			flushSync();

			const send = host.querySelector<HTMLButtonElement>('button.send-btn');
			const modelChip = host.querySelector<HTMLButtonElement>('button.model-chip');
			expect(send?.disabled).toBe(false);
			expect(send?.title).toBe('Queues until model switch finishes');
			expect(modelChip?.classList.contains('unloaded')).toBe(true);
			expect(modelChip?.title).toBe('Model switch in progress');
		},
	);

	it.each([
		'running',
		'captured',
		'installed',
		'overtaken',
		'failed',
		'cancelled',
	] as const)('keeps lifecycle status out of the context chip when checkpoint state is %s', async (status) => {
		setCheckpoint(status);
		setContextUsage('session-ctx', measuredContextReceipt(200));
		const fetchMock = vi.fn(async () => previewResponse());
		vi.stubGlobal('fetch', fetchMock);
		mountContextRow();

		const contextButton = host.querySelector<HTMLButtonElement>('button.context-chip');
		expect(contextButton?.textContent).toContain('20%');
		expect(contextButton?.title).toBe('Context window');
		expect(contextButton?.textContent).not.toMatch(/Checkpoint|Switching|queued/);
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('uses the safe-boundary receipt until the exact context sheet opens', async () => {
		setContextUsage('session-ctx', measuredContextReceipt(600));
		const fetchMock = vi.fn(async () => (
			contextPreviewResponse(200, 'estimated', 'checkpoint-preview')
		));
		vi.stubGlobal('fetch', fetchMock);
		mountContextRow();

		const chip = host.querySelector<HTMLButtonElement>('button.context-chip');
		expect(chip?.textContent).toContain('60%');
		const marker = chip?.querySelector<HTMLElement>('.ctx-estimate-marker');
		expect(marker?.classList.contains('estimated')).toBe(false);
		expect(chip?.title).toBe('Context window');
		expect(fetchMock).not.toHaveBeenCalled();
		setContextUsage('session-ctx', measuredContextReceipt(700));
		flushSync();
		expect(chip?.textContent).toContain('70%');
		expect(fetchMock).not.toHaveBeenCalled();

		if (component) unmount(component);
		component = null;
		host.replaceChildren();
		mountContextControls();

		await vi.waitFor(() => {
			expect(fetchMock).toHaveBeenCalledTimes(1);
			const headline = host.querySelector<HTMLElement>('.ctx-headline');
			const historyRow = host.querySelector<HTMLElement>(
				'.ctx-source-row[title="Chat history"]',
			);
			expect(headline?.textContent).toContain('Estimated');
			expect(headline?.textContent).toContain('200');
			expect(headline?.textContent).not.toContain('600');
			expect(historyRow?.textContent).toContain('200 tokens');
			expect(historyRow?.textContent).not.toContain('540 tokens');
		});
	});

	it('refreshes the preview when the same snapshot changes from stale to measured', async () => {
		setContextUsage('session-ctx', {
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 100,
			user_tokens: 0,
			total_used: 100,
			utilization: 0.1,
			token_usage_available: true,
			token_source: 'measured',
			snapshot_id: 'same-provider-boundary',
			was_trimmed: false,
			history_messages: 1,
		} as never);
		let previewCalls = 0;
		let resolveStale: (
			(value: ReturnType<typeof contextPreviewResponse>) => void
		) | undefined;
		let resolveMeasured: (
			(value: ReturnType<typeof contextPreviewResponse>) => void
		) | undefined;
		vi.stubGlobal('fetch', vi.fn(async () => {
			previewCalls += 1;
			if (previewCalls === 1) {
				return contextPreviewResponse(100, 'measured', 'same-provider-boundary');
			}
			return new Promise<ReturnType<typeof contextPreviewResponse>>((resolve) => {
				if (previewCalls === 2) resolveStale = resolve;
				else resolveMeasured = resolve;
			});
		}));
		mountContextControls();

		await vi.waitFor(() => {
			expect(previewCalls).toBe(1);
			expect(host.querySelector<HTMLElement>('.ctx-headline')?.textContent)
				.toContain('100');
		});

		setContextUsage('session-ctx', {
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: null,
			utilization: null,
			token_usage_available: false,
			snapshot_id: 'same-provider-boundary',
			was_trimmed: false,
			history_messages: 0,
		} as never);
		flushSync();
		await vi.waitFor(() => expect(previewCalls).toBe(2));

		setContextUsage('session-ctx', {
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 600,
			user_tokens: 0,
			total_used: 600,
			utilization: 0.6,
			token_usage_available: true,
			token_source: 'measured',
			snapshot_id: 'same-provider-boundary',
			was_trimmed: false,
			history_messages: 2,
		} as never);
		flushSync();
		// The stale read remains the one request in flight; the measured identity
		// is queued and starts only after that read settles.
		expect(previewCalls).toBe(2);
		resolveStale?.(
			contextPreviewResponse(200, 'estimated', 'same-provider-boundary'),
		);
		await vi.waitFor(() => expect(previewCalls).toBe(3));
		resolveMeasured?.(
			contextPreviewResponse(600, 'measured', 'same-provider-boundary'),
		);

		await vi.waitFor(() => {
			expect(host.querySelector<HTMLElement>('.ctx-headline')?.textContent)
				.toContain('600');
		});
		expect(host.querySelector<HTMLElement>('.ctx-headline')?.textContent)
			.toContain('60%');
	});

	it('keeps the closed context chip on its safe-boundary receipt', () => {
		setContextUsage('session-ctx', measuredContextReceipt(600));
		const fetchMock = vi.fn(async () => previewResponse());
		vi.stubGlobal('fetch', fetchMock);
		mountContextRow();

		setCheckpoint('installed', 'manual');
		flushSync();

		const chip = host.querySelector<HTMLButtonElement>('button.context-chip');
		expect(chip?.textContent).toContain('60%');
		expect(chip?.querySelector('.ctx-ring.loading')).toBeNull();
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('keeps checkpoint controls visible while the sheet preview refreshes', async () => {
		setContextUsage('session-ctx', measuredContextReceipt(600));
		let previewCalls = 0;
		vi.stubGlobal('fetch', vi.fn(async () => {
			previewCalls += 1;
			if (previewCalls === 1) {
				return contextPreviewResponse(600, 'measured', 'before-checkpoint');
			}
			return new Promise<ReturnType<typeof contextPreviewResponse>>(() => {});
		}));
		mountContextControls();

		await vi.waitFor(() => {
			expect(host.querySelector('.ctx-headline')).not.toBeNull();
		});

		setCheckpoint('running', 'manual');
		flushSync();

		await vi.waitFor(() => expect(previewCalls).toBe(2));
		// The previous preview stays on screen and is refreshed in place (#1953).
		expect(host.textContent).not.toContain('Loading context…');
		expect(host.textContent).toContain('Updating…');
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		expect(host.textContent).not.toContain('Usage unavailable');
		expect(host.querySelector('[data-checkpoint-cancel="checkpoint-1"]')).not.toBeNull();
	});

	it('opens on the last measured usage without waiting for the preview', async () => {
		setContextUsage('session-ctx', measuredContextReceipt(600));
		vi.stubGlobal('fetch', vi.fn(() => new Promise<never>(() => {})));
		mountContextControls();

		await vi.waitFor(() => {
			expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		});
		expect(host.textContent).not.toContain('Loading context');
		expect(host.textContent).toContain('Updating…');
	});

	it('does not show another thread\'s last measured usage while its preview loads', async () => {
		setContextUsage('session-ctx', { ...measuredContextReceipt(600), thread_id: 'some-other-thread' });
		vi.stubGlobal('fetch', vi.fn(() => new Promise<never>(() => {})));
		mountContextControls();

		await vi.waitFor(() => expect(host.textContent).toContain('Loading context'));
		expect(host.querySelector('.ctx-headline')).toBeNull();
	});

	it.each(['installed', 'failed'] as const)(
		'refreshes the preview when same-model Checkpoint Now becomes %s',
		async (terminalStatus) => {
			setCheckpoint('captured', 'manual');
			let previewCalls = 0;
			vi.stubGlobal('fetch', vi.fn(async () => {
				previewCalls += 1;
				return contextPreviewResponse(
					previewCalls > 1 ? 300 : 200,
					'estimated',
					'same-checkpoint-boundary',
				);
			}));
			mountContextControls();

			await vi.waitFor(() => expect(previewCalls).toBe(1));
			setCheckpoint(terminalStatus, 'manual');
			flushSync();

			await vi.waitFor(() => {
				expect(previewCalls).toBe(2);
				const previewText = host.querySelector<HTMLElement>(
					'.ctx-headline',
				)?.textContent;
				expect(previewText).toContain('300');
				expect(previewText).not.toContain('Checkpoint failed');
			});
		},
	);

	it('uses the backend occupancy total instead of summing attribution rows', async () => {
		clearAllContextUsage();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/api/threads/session-ctx/context-snapshot/next-turn')) {
				return {
					ok: true,
					json: async () => ({
						preview: {
							model_context_window: 200000,
							system_tokens: 20000,
							tool_def_tokens: 0,
							history_tokens: 37637,
							user_tokens: 100,
							total_used: 86335,
							utilization: 0.432,
							token_source: 'measured',
							preview: true,
							was_trimmed: false,
							history_messages: 256,
							snapshot_id: 'math-preview',
							context_snapshot: {
								snapshot_id: 'math-preview',
								content_hash: 'mathabcdef',
								delivery_leaves: [],
								source_inventory: [
									{
										source_id: 'system:bootloader',
										origin_section: 'system',
										label: 'System context: AGENTS.md',
										stable_reference: '/workspace/AGENTS.md',
										freshness: 'fresh',
										inclusion: 'included',
										owner_surface: 'document-viewer',
										prompt_tokens: 20000,
									},
									{
										source_id: 'project:bootloader',
										origin_section: 'project',
										label: 'Project context: PROJECT.md',
										stable_reference: '/workspace/PROJECT.md',
										freshness: 'fresh',
										inclusion: 'included',
										owner_surface: 'document-viewer',
										prompt_tokens: 19826,
									},
									{
									source_id: 'dynamic:agent-roster',
										origin_section: 'dynamic',
									label: 'Agent Responsibility Map',
									stable_reference: 'dynamic://agents',
										freshness: 'fresh',
										inclusion: 'included',
									owner_surface: 'agent-roster',
									metadata: { provider: 'agent_owner_map', agent_count: 3 },
										prompt_tokens: 324,
									},
								],
							},
							source_artifacts: [],
						},
					}),
				};
			}
			throw new Error(`unexpected fetch ${url}`);
		}));

		mountContextControls();

		await vi.waitFor(() => {
			const headline = host.querySelector<HTMLElement>('.ctx-headline');
			expect(headline?.textContent).toContain('86k');
			expect(headline?.textContent).toContain('43%');
			expect(headline?.textContent).not.toContain('39%');
		});
	});

	it('keeps the context chip as an isolated sheet trigger', async () => {
		const toggleSheet = vi.fn();
		const form = document.createElement('form');
		const submit = vi.fn((event: SubmitEvent) => event.preventDefault());
		form.addEventListener('submit', submit);
		document.body.appendChild(form);
		form.appendChild(host);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/api/threads/session-ctx/context-snapshot/next-turn')) {
				return {
					ok: true,
					json: async () => ({
						preview: {
							model_context_window: 1000,
							system_tokens: 0,
							tool_def_tokens: 0,
							history_tokens: 10,
							user_tokens: 0,
							total_used: 10,
							utilization: 0.01,
							was_trimmed: false,
							history_messages: 1,
							snapshot_id: 'ctx-preview',
							context_snapshot: {
								snapshot_id: 'ctx-preview',
								content_hash: 'abcdef1234567890',
								delivery_leaves: [],
								source_inventory: [],
							},
							source_artifacts: [],
						},
					}),
				};
			}
			throw new Error(`unexpected fetch ${url}`);
		}));
		component = mount(InputBarControls, {
			target: host,
			props: {
				surface: 'row',
				sheetView: null,
				toggleSheet,
				isMobile: false,
				hasImageAttachments: false,
				inputText: '',
				pendingAttachmentCount: 0,
				hasMessageToSend: false,
				canSendToModel: true,
				queueUntilSwitch: false,
				onAttachFile: () => {},
				onAttachCamera: () => {},
				onAttachPhotos: () => {},
				onSendClick: () => {}, onQueueAfterTurn: () => {},
			},
		});
		flushSync();

		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('button.context-chip')).toBeTruthy());
		const contextButton = host.querySelector<HTMLButtonElement>('button.context-chip');
		expect(contextButton?.type).toBe('button');
		contextButton?.click();

		expect(toggleSheet).toHaveBeenCalledWith('context');
		expect(submit).not.toHaveBeenCalled();
		form.remove();
	});

	it('hides the composer context control when no model is selected', () => {
		activeModelId.set(null);
		const toggleSheet = vi.fn();
		component = mount(InputBarControls, {
			target: host,
			props: {
				surface: 'row',
				sheetView: null,
				toggleSheet,
				isMobile: false,
				hasImageAttachments: false,
				inputText: '',
				pendingAttachmentCount: 0,
				hasMessageToSend: false,
				canSendToModel: true,
				queueUntilSwitch: false,
				onAttachFile: () => {},
				onAttachCamera: () => {},
				onAttachPhotos: () => {},
				onSendClick: () => {}, onQueueAfterTurn: () => {},
			},
		});
		flushSync();

		expect(host.querySelector<HTMLButtonElement>('button.context-chip')).toBeNull();
	});

	it('shows next-turn preview only and does not request latest delivered context', async () => {
		threads.set([{
			id: 'session-ctx',
			title: 'CTX Thread',
			bootloader_files: ['/Users/alice/Documents/projects/corsia/working/proof/thread-context.md'],
		} as never]);
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/api/threads/session-ctx/context-snapshot/next-turn')) {
				return {
					ok: true,
					json: async () => ({
						preview: {
							model_context_window: 1000,
							system_tokens: 0,
							tool_def_tokens: 0,
							history_tokens: 12,
							user_tokens: 0,
							total_used: 12,
							utilization: 0.012,
							was_trimmed: false,
							history_messages: 1,
							snapshot_id: 'ctx-preview',
							context_snapshot: {
								snapshot_id: 'ctx-preview',
								content_hash: 'previewabcdef123456',
								delivery_leaves: [],
								source_inventory: [
									{
										source_id: 'summary:preview',
										origin_section: 'summary',
										label: 'Preview Summary',
										stable_reference: 'summary://preview',
										freshness: 'fresh',
										inclusion: 'included',
										reason: 'same-scope recent inactive thread',
										owner_surface: 'summary-details',
									},
								],
							},
							source_artifacts: [],
						},
					}),
				};
			}
			throw new Error(`unexpected fetch ${url}`);
			});
			vi.stubGlobal('fetch', fetchMock);

			mountContextControls();

			await vi.waitFor(() => expect(host.textContent).toContain('Preview Summary'));
			expect(host.querySelector('details.thread-prefix-panel')).toBeNull();
			expect(host.textContent).not.toContain('Thread prefix');
			expect(host.textContent).not.toContain('/Users/alice/Documents/projects/corsia/working/proof/thread-context.md');
			expect(host.textContent).not.toContain('Preview for the next send');
			expect(host.textContent).not.toContain('Delivered Summary');
			expect([...host.querySelectorAll<HTMLButtonElement>('button')]
				.some((button) => button.textContent?.trim() === 'Last delivered')).toBe(false);
			expect([...host.querySelectorAll<HTMLButtonElement>('button')]
				.some((button) => button.textContent?.trim() === 'Next turn')).toBe(false);
			expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
				expect.stringContaining('/context-snapshot/next-turn'),
			]);
		});

	it('opens document-owned source rows in the document viewer', async () => {
		const sourcePath = '/Users/alice/Documents/projects/corsia/PROJECT.md';
			const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
				const url = String(input);
				if (url.includes('/api/threads/session-ctx/context-snapshot/next-turn')) {
				return {
					ok: true,
					json: async () => ({
						preview: {
							model_context_window: 1000,
							system_tokens: 0,
							tool_def_tokens: 0,
							history_tokens: 12,
							user_tokens: 0,
							total_used: 12,
							utilization: 0.012,
							was_trimmed: false,
							history_messages: 1,
							snapshot_id: 'ctx-preview',
							context_snapshot: {
								snapshot_id: 'ctx-preview',
								content_hash: 'abcdef1234567890',
								delivery_leaves: [],
								source_inventory: [
									{
										source_id: 'project-bootloader:p1:PROJECT.md',
										origin_section: 'project',
										label: 'Project bootloader: PROJECT.md',
										stable_reference: sourcePath,
										freshness: 'fresh',
										inclusion: 'included',
										reason: 'project bootloader file',
										owner_surface: 'document-viewer',
										metadata: { project_id: 'p1' },
									},
								],
							},
							source_artifacts: [],
						},
					}),
				};
			}
			throw new Error(`unexpected fetch ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		mountContextControls();

		await vi.waitFor(() => expect(host.textContent).toContain('PROJECT.md'));
		expect(host.textContent).not.toContain('Project bootloader: PROJECT.md');
		const action = host.querySelector<HTMLButtonElement>(
			'button[aria-label="Open Document for PROJECT.md"]'
		);
		expect(action).toBeTruthy();
		action?.click();

		const tabs = get(threadWorkspaces)['session-ctx']?.tabs ?? [];
		expect(tabs).toHaveLength(1);
		expect(tabs[0]).toMatchObject({ path: sourcePath, title: 'PROJECT.md' });
	});
});

describe('the donut chip and the sheet headline share the sheet\'s preview (#1952)', () => {
	// InputBarControls mounts twice — surface="row" for the composer donut,
	// surface="sheets" for the open sheet's own content — and each used to
	// fetch and hold its own preview independently. Only the sheets instance
	// ever fetches, so the row's chip fell back to the last *measured* usage
	// and could show a different number than the sheet right after a
	// checkpoint, when the sheet switches to an estimated preview.
	it('publishes the fetched preview so a sibling row instance can read it', async () => {
		setContextUsage('session-ctx', measuredContextReceipt(500) as never);
		vi.stubGlobal(
			'fetch',
			vi.fn(async () => contextPreviewResponse(50, 'estimated', 'post-checkpoint')),
		);

		mountContextControls();
		await vi.waitFor(() => expect(host.textContent).toContain('Estimated'));

		expect(get(contextPreviewByThread).get('session-ctx')?.usage?.snapshot_id).toBe('post-checkpoint');
		expect(get(contextPreviewByThread).get('session-ctx')?.usage?.total_used).toBe(50);

		unmount(component as never);
		component = null;
		mountContextRow();

		const chip = host.querySelector<HTMLButtonElement>('button.context-chip');
		// The chip now reads the same 50/1000 preview instead of the 500/1000
		// measured receipt still sitting in contextUsage.
		expect(chip?.title).toBe('Estimated next-turn context');
	});
});

describe('context chip and sheet agree on one percentage', () => {
	// The stored utilization is rounded to three decimals by the backend, so
	// rounding it again for the chip while the sheet recomputes from raw tokens
	// lets the two disagree by a point for the same usage (#1413). 4.6 tokens of
	// 1000 is the reported worked example: 0.0046 stored as 0.005 rounds up to
	// 1% for the chip, while 4.6/1000 rounds down to 0% for the sheet.
	function preRoundedReceipt() {
		return {
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 0,
			user_tokens: 0,
			total_used: 4.6,
			utilization: 0.005,
			token_source: 'measured' as const,
			token_usage_available: true,
			snapshot_id: 'boundary-rounding',
			continuity_mode: 'full' as const,
			context_snapshot: { source_inventory: [] },
		};
	}

	it('shows the same percentage the sheet computes, with no inventory', () => {
		const usage = preRoundedReceipt();
		setContextUsage('session-ctx', usage as never);
		mountContextRow();

		const chip = host.querySelector<HTMLButtonElement>('button.context-chip');
		const sheet = contextSheetUsageSummary(usage as never);

		expect(sheet.pct).toBe(0);
		expect(chip?.textContent).toContain(`${sheet.pct}%`);
		expect(chip?.textContent).not.toContain('1%');
	});

	it('still agrees when an inventory is present', () => {
		const usage = { ...preRoundedReceipt(), total_used: 600, utilization: 0.6 };
		setContextUsage('session-ctx', usage as never);
		mountContextRow();

		const chip = host.querySelector<HTMLButtonElement>('button.context-chip');
		expect(chip?.textContent).toContain(`${contextSheetUsageSummary(usage as never).pct}%`);
	});
});
