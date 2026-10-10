import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { activeThreadId, threads } from '$lib/stores/threads';
import { activeModelId, models } from '$lib/stores/models';
import { subagentMeta } from '$lib/stores/subagents';
import {
	clearAllContextUsage,
	contextPreviewByThread,
	setContextUsage,
} from '$lib/stores/contextUsageByThread';

// Two composers on screen at once: the active thread's and a subagent
// conversation's in the artifact leaf (#2269, #1949).
let host: HTMLElement;
let mounted: Record<string, unknown>[] = [];

const shared = {
	toggleSheet: () => {}, isMobile: false, hasImageAttachments: false, inputText: '',
	pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true,
	queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
	onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
};

function usage(threadId: string, total: number) {
	return {
		model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
		history_tokens: total, user_tokens: 0, total_used: total, utilization: total / 1000,
		token_source: 'measured' as const, token_usage_available: true,
		snapshot_id: `snap-${threadId}-${total}`, was_trimmed: false,
		history_messages: 2, thread_id: threadId,
	};
}

function previewFor(total: number) {
	return { ...usage('', total), thread_id: undefined, preview: true };
}

function mountComposer(threadId: string, surface: 'row' | 'sheets', sheetView: 'context' | null = null) {
	const panel = document.createElement('div');
	panel.dataset.panel = `${threadId}-${surface}`;
	host.appendChild(panel);
	mounted.push(mount(InputBarControls, {
		target: panel,
		props: { ...shared, threadId, surface, sheetView },
	}));
	flushSync();
	return panel;
}

function chipOf(panel: HTMLElement) {
	return panel.querySelector<HTMLButtonElement>('button.context-chip');
}

beforeEach(() => {
	token.set('test-token');
	connectionState.set('connected');
	activeThreadId.set('parent');
	threads.set([
		{ id: 'parent', title: 'Parent chat' } as never,
		{ id: 'worker', title: 'worker-thread' } as never,
	]);
	subagentMeta.set(new Map([['worker', {
		childThreadId: 'worker', parentThreadId: 'parent', name: 'Worker bee',
		provider: 'cc', status: 'running',
	}]]));
	activeModelId.set('model-ready');
	models.set([{
		id: 'model-ready', name: 'Model-35B', size: '', backend: 'local', speed: '', status: 'ready',
		capabilities: {
			context_window_max: 262144, supports_vision: false, supports_tools: true,
			supports_reasoning: false, reasoning_config: null,
		},
	}]);
	clearAllContextUsage();
	contextPreviewByThread.set(new Map());
	checkpointsByThread.set(new Map());
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	for (const component of mounted) unmount(component);
	mounted = [];
	host.remove();
	vi.unstubAllGlobals();
	token.set(null);
	connectionState.set('disconnected');
	activeThreadId.set(null);
	threads.set([]);
	subagentMeta.set(new Map());
	activeModelId.set(null);
	models.set([]);
	clearAllContextUsage();
	contextPreviewByThread.set(new Map());
});

describe('per-thread context inspection (#2269)', () => {
	it('shows each panel its own usage with no cross-talk, updating independently', () => {
		setContextUsage('parent', usage('parent', 200));
		setContextUsage('worker', usage('worker', 700));
		const parentPanel = mountComposer('parent', 'row');
		const workerPanel = mountComposer('worker', 'row');

		expect(chipOf(parentPanel)?.textContent).toContain('20%');
		expect(chipOf(workerPanel)?.textContent).toContain('70%');

		setContextUsage('worker', usage('worker', 800));
		flushSync();
		expect(chipOf(parentPanel)?.textContent).toContain('20%');
		expect(chipOf(workerPanel)?.textContent).toContain('80%');

		setContextUsage('parent', usage('parent', 300));
		flushSync();
		expect(chipOf(parentPanel)?.textContent).toContain('30%');
		expect(chipOf(workerPanel)?.textContent).toContain('80%');
	});

	it('keeps the leaf chip when the main panel switches threads, and shows no number for an unmeasured thread', () => {
		setContextUsage('parent', usage('parent', 200));
		setContextUsage('worker', usage('worker', 700));
		const parentPanel = mountComposer('parent', 'row');
		const workerPanel = mountComposer('worker', 'row');

		threads.update(rows => [...rows, { id: 'other', title: 'Other' } as never]);
		activeThreadId.set('other');
		flushSync();

		expect(chipOf(workerPanel)?.textContent).toContain('70%');
		expect(chipOf(parentPanel)?.textContent).toContain('20%');
		const otherPanel = mountComposer('other', 'row');
		expect(chipOf(otherPanel)).not.toBeNull();
		expect(chipOf(otherPanel)?.textContent).not.toContain('%');
	});

	it('names each chip and sheet after its own thread', () => {
		const parentPanel = mountComposer('parent', 'row');
		const workerPanel = mountComposer('worker', 'row');
		expect(chipOf(parentPanel)?.getAttribute('aria-label')).toBe('Context for Parent chat');
		expect(chipOf(workerPanel)?.getAttribute('aria-label')).toBe('Context for Worker bee');

		setContextUsage('worker', usage('worker', 700));
		const sheet = mountComposer('worker', 'sheets', 'context');
		expect(sheet.querySelector('[aria-label="Context for Worker bee"]')).not.toBeNull();
	});

	it("fetches the worker's own preview without the parent's model and stores it under the worker", async () => {
		const fetchMock = vi.fn(async (input: string) => ({
			ok: true,
			json: async () => ({ preview: input.includes('/worker/') ? previewFor(450) : previewFor(150) }),
		}));
		vi.stubGlobal('fetch', fetchMock);
		const parentRow = mountComposer('parent', 'row');
		const workerRow = mountComposer('worker', 'row');
		mountComposer('worker', 'sheets', 'context');

		await vi.waitFor(() => {
			expect(fetchMock).toHaveBeenCalledTimes(1);
			const url = String(fetchMock.mock.calls[0][0]);
			expect(url).toContain('/api/threads/worker/context-snapshot/next-turn');
			expect(url).not.toContain('model=');
		});
		await vi.waitFor(() => {
			expect(chipOf(workerRow)?.textContent).toContain('45%');
		});
		// The parent chip is untouched by the worker's preview.
		expect(chipOf(parentRow)?.textContent).not.toContain('45%');
		expect(chipOf(parentRow)?.textContent).not.toContain('15%');
	});

	it('still previews the active thread with the selected model', async () => {
		const fetchMock = vi.fn(async (_url: string) => ({
			ok: true, json: async () => ({ preview: previewFor(150) }),
		}));
		vi.stubGlobal('fetch', fetchMock);
		mountComposer('parent', 'sheets', 'context');
		await vi.waitFor(() => {
			expect(fetchMock).toHaveBeenCalledTimes(1);
			expect(String(fetchMock.mock.calls[0][0])).toContain('model=model-ready');
		});
	});
});
