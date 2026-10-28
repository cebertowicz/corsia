import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId, threads } from '$lib/stores/threads';
import { activeModelId, models } from '$lib/stores/models';
import { clearAllContextUsage, contextPreviewByThread, recordMeasuredUsage, setContextUsage } from '$lib/stores/contextUsageByThread';
import { handleContextUsageUpdated, handleTurnActivity, handleTurnComplete } from '$lib/ws/handlersTurns';
import { _resetTurnStateForTests } from '$lib/ws/turnState';

// A source group the user opened stays open while the preview is replaced
// within the same open context view, even when the reading in between has no
// snapshot or no usage at all. Opening it again later, another thread, or a
// source detail visit starts collapsed.

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('test-token');
	connectionState.set('connected');
	activeThreadId.set('session-ctx');
	threads.set([
		{ id: 'session-ctx', title: 'CTX Thread' } as never,
		{ id: 'another-session', title: 'Another Thread' } as never,
	]);
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
	_resetTurnStateForTests();
	threadWorkspaces.set({});
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	token.set(null);
	connectionState.set('disconnected');
	activeThreadId.set(null);
	threads.set([]);
	activeModelId.set(null);
	models.set([]);
	clearAllContextUsage();
	contextPreviewByThread.set(new Map());
	checkpointsByThread.set(new Map());
	_resetTurnStateForTests();
	threadWorkspaces.set({});
});

function previewResponse(snapshotId: string) {
	return {
		ok: true,
		json: async () => ({
			preview: {
				model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
				history_tokens: 600, user_tokens: 0, total_used: 600,
				utilization: 0.6, token_source: 'measured', token_usage_available: true,
				snapshot_id: snapshotId, was_trimmed: false, history_messages: 2,
				context_snapshot: {
					snapshot_id: snapshotId,
					source_inventory: [
						{
							source_id: 'project-note', origin_section: 'project', label: 'Project note',
							stable_reference: 'ctx://note', freshness: 'fresh', inclusion: 'included',
							reason: 'project note', owner_surface: 'runtime-details',
						},
						{
							source_id: 'history-recent', origin_section: 'history', label: 'Recent history',
							stable_reference: 'ctx://history', freshness: 'fresh', inclusion: 'included',
							reason: 'history', owner_surface: 'runtime-details',
						},
					],
				},
			},
		}),
	};
}

/** A measured reading with numbers but no `context_snapshot`: no source list. */
const measuredNoSnapshot = {
	model_context_window: 1000, system_tokens: 40, tool_def_tokens: 0,
	history_tokens: 700, user_tokens: 0, total_used: 720,
	utilization: 0.72, token_source: 'measured', token_usage_available: true,
	snapshot_id: 'boundary-720', continuity_mode: 'full', was_trimmed: false,
	history_messages: 4, thread_id: 'session-ctx',
};

/** A reading taken for another thread, which this composer may not show. */
const foreignReading = { ...measuredNoSnapshot, snapshot_id: 'boundary-foreign', thread_id: 'foreign-thread' };

function mountControls(sheetView: 'context' | null = 'context') {
	const props = $state<Record<string, unknown>>({
		surface: 'sheets', sheetView, toggleSheet: () => {},
		isMobile: false, hasImageAttachments: false, inputText: '',
		pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true,
		queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
		onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
	});
	component = mount(InputBarControls, { target: host, props: props as never });
	flushSync();
	return props;
}

function groups(): HTMLDetailsElement[] {
	return [...host.querySelectorAll<HTMLDetailsElement>('details.ctx-source-group')];
}

function group(name: string): HTMLDetailsElement {
	const found = groups().find((el) => el.querySelector('summary')?.textContent?.includes(name));
	if (!found) throw new Error(`no ${name} group`);
	return found;
}

/** Click the summary, then let the browser's own toggle event land, as a user's later action would. */
async function openGroup(name: string) {
	group(name).querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
	flushSync();
	expect(group(name).open).toBe(true);
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

/** Resolves the Nth fetch by hand so the intermediate reading can be inspected. */
function deferredFetches() {
	const pending: Array<(value: unknown) => void> = [];
	let calls = 0;
	vi.stubGlobal('fetch', vi.fn(() => {
		calls += 1;
		return new Promise((resolve) => { pending.push(resolve); });
	}));
	return {
		calls: () => calls,
		resolve: (index: number, value: unknown) => pending[index](value),
	};
}

async function loadFirstPreview(fetches: ReturnType<typeof deferredFetches>) {
	await vi.waitFor(() => expect(fetches.calls()).toBe(1));
	fetches.resolve(0, previewResponse('preview-1'));
	await vi.waitFor(() => expect(groups().length).toBeGreaterThan(0));
}

describe('context sheet preview requests during a multi-step turn', () => {
	function contextFrame(snapshotId: string, totalUsed: number) {
		return {
			...measuredNoSnapshot,
			snapshot_id: snapshotId,
			total_used: totalUsed,
		};
	}

	it('keeps one read in flight and coalesces later frames only while the sheet is open', async () => {
		const fetches = deferredFetches();
		mountControls();
		await loadFirstPreview(fetches);

		handleTurnActivity({ type: 'turn_activity', thread_id: 'session-ctx', turn_id: 'many-steps', turn_order: 1 });
		handleContextUsageUpdated({
			type: 'context_usage_updated', thread_id: 'session-ctx', turn_id: 'many-steps', turn_order: 1,
			context_usage: contextFrame('step-1', 741),
		});
		flushSync();
		await vi.waitFor(() => expect(fetches.calls()).toBe(2));

		for (const [snapshotId, totalUsed] of [['step-2', 742], ['step-3', 743]] as const) {
			handleContextUsageUpdated({
				type: 'context_usage_updated', thread_id: 'session-ctx', turn_id: 'many-steps', turn_order: 1,
				context_usage: contextFrame(snapshotId, totalUsed),
			});
			flushSync();
		}
		handleTurnComplete({
			type: 'turn_complete', thread_id: 'session-ctx', turn_id: 'many-steps', turn_order: 1,
			context_usage: contextFrame('complete', 744),
		});
		flushSync();

		// Four frames share one in-flight read; the prior preview stays visible.
		expect(fetches.calls()).toBe(2);
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');

		fetches.resolve(1, previewResponse('stale-preview'));
		await vi.waitFor(() => expect(fetches.calls()).toBe(3));
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
		fetches.resolve(2, previewResponse('complete-preview'));
		await vi.waitFor(() => expect(groups().length).toBeGreaterThan(0));
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
	});

	it('refreshes once after an unmeasured frame invalidates an otherwise identical in-flight read', async () => {
		setContextUsage('session-ctx', measuredNoSnapshot as never);
		const fetches = deferredFetches();
		mountControls();
		await vi.waitFor(() => expect(fetches.calls()).toBe(1));

		// `stale` is not part of the preview identity. This used to leave the
		// discarded request's loading entry behind forever.
		recordMeasuredUsage('session-ctx', {
			...measuredNoSnapshot,
			token_usage_available: false,
		} as never);
		flushSync();
		expect(fetches.calls()).toBe(1);

		fetches.resolve(0, previewResponse('discarded-preview'));
		await vi.waitFor(() => expect(fetches.calls()).toBe(2));
		fetches.resolve(1, previewResponse('fresh-preview'));
		await vi.waitFor(() => expect(get(contextPreviewByThread).get('session-ctx')?.loading).toBe(false));
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('60%');
	});

	it('does not refresh the preview for those frames while the sheet is closed', () => {
		const fetches = deferredFetches();
		mountControls(null);

		handleTurnActivity({ type: 'turn_activity', thread_id: 'session-ctx', turn_id: 'closed', turn_order: 1 });
		handleContextUsageUpdated({
			type: 'context_usage_updated', thread_id: 'session-ctx', turn_id: 'closed', turn_order: 1,
			context_usage: contextFrame('closed-step', 741),
		});
		handleTurnComplete({
			type: 'turn_complete', thread_id: 'session-ctx', turn_id: 'closed', turn_order: 1,
			context_usage: contextFrame('closed-complete', 744),
		});
		flushSync();

		expect(fetches.calls()).toBe(0);
	});
});

describe('context sheet group expansion across a preview refresh', () => {
	it('keeps an opened group open through a reading with no context_snapshot', async () => {
		const fetches = deferredFetches();
		mountControls();
		await loadFirstPreview(fetches);
		await openGroup('Project');

		// A turn completes: a measured reading without a snapshot replaces the
		// preview until the next one lands.
		setContextUsage('session-ctx', measuredNoSnapshot as never);
		flushSync();
		await vi.waitFor(() => expect(fetches.calls()).toBe(2));
		expect(groups()).toHaveLength(0);
		expect(host.querySelector('.ctx-headline')?.textContent).toContain('72%');

		fetches.resolve(1, previewResponse('preview-2'));
		await vi.waitFor(() => expect(groups().length).toBeGreaterThan(0));
		expect(group('Project').open).toBe(true);
		expect(group('History').open).toBe(false);
	});

	it('renders source groups from a measured live payload without waiting for preview', async () => {
		const fetches = deferredFetches();
		mountControls();
		await vi.waitFor(() => expect(fetches.calls()).toBe(1));

		recordMeasuredUsage('session-ctx', {
			...measuredNoSnapshot,
			context_snapshot: {
				snapshot_id: 'measured-live',
				source_inventory: [{
					source_id: 'live-project', origin_section: 'project', label: 'Live project',
					stable_reference: 'ctx://live', freshness: 'fresh', inclusion: 'included',
					reason: 'live', owner_surface: 'runtime-details',
				}],
			},
		} as never);
		flushSync();

		expect(groups().length).toBeGreaterThan(0);
		expect(group('Project').textContent).toContain('Live project');
	});

	it('keeps preview source groups while a live payload refreshes them', async () => {
		const fetches = deferredFetches();
		mountControls();
		await loadFirstPreview(fetches);
		recordMeasuredUsage('session-ctx', {
			...measuredNoSnapshot,
			context_snapshot: {
				snapshot_id: 'measured-live',
				source_inventory: [{
					source_id: 'live-project', origin_section: 'project', label: 'Live project',
					stable_reference: 'ctx://live', freshness: 'fresh', inclusion: 'included',
					reason: 'live', owner_surface: 'runtime-details',
				}],
			},
		} as never);
		flushSync();
		await vi.waitFor(() => expect(fetches.calls()).toBe(2));
		expect(group('Project').textContent).toContain('Project note');
	});

	it('keeps an opened group open when the reading is null and a checkpoint state holds the sheet', async () => {
		const fetches = deferredFetches();
		checkpointsByThread.set(new Map([['session-ctx', { checkpoints: [], loading: false, error: null }]]));
		mountControls();
		await loadFirstPreview(fetches);
		await openGroup('Project');

		// The new owner has no usable reading yet, so the sheet has no usage.
		setContextUsage('session-ctx', foreignReading as never);
		flushSync();
		await vi.waitFor(() => expect(fetches.calls()).toBe(2));
		expect(host.querySelector('.ctx-headline')).toBeNull();
		expect(groups()).toHaveLength(0);

		fetches.resolve(1, previewResponse('preview-2'));
		await vi.waitFor(() => expect(groups().length).toBeGreaterThan(0));
		expect(group('Project').open).toBe(true);
	});

	it('keeps an opened group open when the reading is null and nothing else holds the sheet', async () => {
		const fetches = deferredFetches();
		mountControls();
		await loadFirstPreview(fetches);
		await openGroup('Project');

		setContextUsage('session-ctx', foreignReading as never);
		flushSync();
		await vi.waitFor(() => expect(fetches.calls()).toBe(2));
		expect(host.textContent).toContain('Loading context');
		expect(groups()).toHaveLength(0);

		fetches.resolve(1, previewResponse('preview-2'));
		await vi.waitFor(() => expect(groups().length).toBeGreaterThan(0));
		expect(group('Project').open).toBe(true);
	});
});

describe('context sheet group expansion resets', () => {
	it('starts every group collapsed on another thread', async () => {
		const fetches = deferredFetches();
		mountControls();
		await loadFirstPreview(fetches);
		await openGroup('Project');

		activeThreadId.set('another-session');
		flushSync();
		await vi.waitFor(() => expect(fetches.calls()).toBe(2));
		fetches.resolve(1, previewResponse('preview-other'));
		await vi.waitFor(() => expect(groups().length).toBeGreaterThan(0));
		expect(groups().every((el) => !el.open)).toBe(true);
	});

	it('starts every group collapsed when the context view is closed and opened again', async () => {
		const fetches = deferredFetches();
		const props = mountControls();
		await loadFirstPreview(fetches);
		await openGroup('Project');

		props.sheetView = null;
		flushSync();
		expect(host.querySelector('.context-sheet')).toBeNull();
		props.sheetView = 'context';
		flushSync();

		// The kept preview shows at once on reopen; nothing is read again.
		expect(groups().length).toBeGreaterThan(0);
		expect(groups().every((el) => !el.open)).toBe(true);
	});

	it('starts every group collapsed after a source detail visit', async () => {
		const fetches = deferredFetches();
		mountControls();
		await loadFirstPreview(fetches);
		await openGroup('Project');
		await openGroup('History');

		// The history row has no owner settings surface, so it opens its detail here.
		host.querySelector<HTMLButtonElement>('button[aria-label$="for Recent history"]')?.click();
		flushSync();
		expect(host.querySelector('.context-source-back')).not.toBeNull();
		expect(groups()).toHaveLength(0);

		host.querySelector<HTMLButtonElement>('.context-source-back')?.click();
		flushSync();
		expect(groups().length).toBeGreaterThan(0);
		expect(groups().every((el) => !el.open)).toBe(true);
	});
});
