import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarControls from './InputBarControls.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId, threads } from '$lib/stores/threads';
import { activeModelId, models } from '$lib/stores/models';
import { subagentMeta } from '$lib/stores/subagents';
import { adoptThreadSnapshot } from '$lib/stores/displayedThreads';
import {
	beginThreadEntry,
	clearAllContextUsage,
	recordMeasuredUsage,
	setContextUsage,
} from '$lib/stores/contextUsageByThread';

// One composer mounts InputBarControls twice: the row (the donut) and the
// always-mounted sheets instance. Both read one per-thread reading, so the
// donut never depends on whether the sheet is open.

let host: HTMLElement;
let mounted: Record<string, unknown>[] = [];
let fetchMock: ReturnType<typeof vi.fn>;
let previewTotal = 300;

const shared = {
	toggleSheet: () => {}, isMobile: false, hasImageAttachments: false, inputText: '',
	pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true,
	queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
	onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
};

function readyModel(id: string) {
	return {
		id, name: id, size: '', backend: 'local', speed: '', status: 'ready',
		capabilities: {
			context_window_max: 262144, supports_vision: false, supports_tools: true,
			supports_reasoning: false, reasoning_config: null,
		},
	};
}

function measured(threadId: string, total: number, extra: Record<string, unknown> = {}) {
	return {
		model_context_window: 1000, system_tokens: 0, tool_def_tokens: 0,
		history_tokens: total, user_tokens: 0, total_used: total,
		utilization: total / 1000, token_source: 'measured', token_usage_available: true,
		snapshot_id: `${threadId}-${total}`, was_trimmed: false, history_messages: 2,
		...extra,
	};
}

function receiptRow(threadId: string, id: string, usage: unknown, model = 'model-a') {
	return {
		id, thread_id: threadId, role: 'assistant' as const, content: 'ok',
		created_at: '2026-10-02T00:00:00Z', metadata: { model, context_usage: usage },
	};
}

function previewCalls(): string[] {
	return fetchMock.mock.calls
		.map(([input]) => String(input))
		.filter(url => url.includes('/context-snapshot/next-turn'));
}

/** The donut and the sheets instance for one composer, sharing reactive props. */
function mountComposer(threadId: string | null = null) {
	const props = $state<Record<string, unknown>>({ ...shared, threadId, sheetView: null });
	const rowHost = document.createElement('div');
	const sheetHost = document.createElement('div');
	host.append(rowHost, sheetHost);
	mounted.push(mount(InputBarControls, { target: rowHost, props: new Proxy(props, {
		get: (target, key) => (key === 'surface' ? 'row' : target[key as string]),
	}) as never }));
	mounted.push(mount(InputBarControls, { target: sheetHost, props: new Proxy(props, {
		get: (target, key) => (key === 'surface' ? 'sheets' : target[key as string]),
	}) as never }));
	flushSync();
	return {
		props,
		chip: () => rowHost.querySelector<HTMLButtonElement>('button.context-chip'),
		donut: () => {
			const chip = rowHost.querySelector<HTMLButtonElement>('button.context-chip');
			const marker = chip?.querySelector('.ctx-estimate-marker');
			const number = chip?.querySelector('.ctx-pct')?.textContent?.replace('~', '') ?? '';
			return `${marker?.classList.contains('estimated') ? '~' : ''}${number}`;
		},
		headline: () => sheetHost.querySelector('.ctx-headline')?.textContent ?? '',
	};
}

beforeEach(() => {
	token.set('test-token');
	connectionState.set('connected');
	activeThreadId.set('thread-a');
	threads.set([{ id: 'thread-a', title: 'A' } as never, { id: 'thread-b', title: 'B' } as never]);
	models.set([readyModel('model-a'), readyModel('model-b')] as never);
	activeModelId.set('model-a');
	clearAllContextUsage();
	checkpointsByThread.set(new Map());
	threadWorkspaces.set({});
	subagentMeta.set(new Map());
	previewTotal = 300;
	fetchMock = vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const thread = url.split('/api/threads/')[1]?.split('/')[0] ?? '';
		return {
			ok: true,
			json: async () => ({
				preview: {
					...measured(thread, previewTotal),
					token_source: 'estimated', token_usage_available: false, preview: true,
					snapshot_id: `preview-${thread}-${previewTotal}`,
				},
			}),
		};
	});
	vi.stubGlobal('fetch', fetchMock);
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
	activeModelId.set(null);
	models.set([]);
	clearAllContextUsage();
	checkpointsByThread.set(new Map());
	subagentMeta.set(new Map());
});

describe('entry hydration (trace rows 1, 2, 8)', () => {
	it('fills the donut from the newest stored receipt before any frame arrives', () => {
		const composer = mountComposer();
		beginThreadEntry('thread-a');
		adoptThreadSnapshot('thread-a', [
			receiptRow('thread-a', 'm1', measured('thread-a', 200)),
			receiptRow('thread-a', 'm2', measured('thread-a', 420)),
		] as never);
		flushSync();

		expect(composer.donut()).toBe('42%');
		expect(previewCalls()).toHaveLength(0);
	});

	it('skips a stored receipt that could not measure', () => {
		const composer = mountComposer();
		beginThreadEntry('thread-a');
		adoptThreadSnapshot('thread-a', [
			receiptRow('thread-a', 'm1', measured('thread-a', 200)),
			receiptRow('thread-a', 'm2', { ...measured('thread-a', 0), token_usage_available: false }),
		] as never);
		flushSync();

		expect(composer.donut()).toBe('20%');
	});

	it('reads the estimate once when the thread has no usable receipt', async () => {
		const composer = mountComposer();
		beginThreadEntry('thread-a');
		flushSync();
		// Nothing is read before the first page says there is no receipt.
		expect(previewCalls()).toHaveLength(0);

		adoptThreadSnapshot('thread-a', [] as never);
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		expect(previewCalls()).toHaveLength(1);
		expect(previewCalls()[0]).toContain('model=model-a');

		// A second adoption of the same page (a reconnect) reads nothing more.
		adoptThreadSnapshot('thread-a', [] as never);
		flushSync();
		await Promise.resolve();
		expect(previewCalls()).toHaveLength(1);
	});

	it('does not show a receipt taken on another model as measured', async () => {
		const composer = mountComposer();
		beginThreadEntry('thread-a');
		adoptThreadSnapshot('thread-a', [
			receiptRow('thread-a', 'm1', measured('thread-a', 420), 'model-b'),
		] as never);
		flushSync();

		expect(composer.donut()).not.toBe('42%');
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
	});
});

describe('open and close (trace rows 3, 4)', () => {
	it('leaves the donut unchanged when the sheet opens and closes, and reopening reads nothing', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		flushSync();
		expect(composer.donut()).toBe('42%');

		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.headline()).toContain('30%'));
		// The donut shows what the headline shows.
		expect(composer.donut()).toBe('~30%');
		expect(previewCalls()).toHaveLength(1);

		composer.props.sheetView = null;
		flushSync();
		expect(composer.donut()).toBe('~30%');

		composer.props.sheetView = 'context';
		flushSync();
		await Promise.resolve();
		expect(composer.headline()).toContain('30%');
		expect(previewCalls()).toHaveLength(1);
	});
});

describe('context-chip readings', () => {
	it('renders semantic estimated and measured readings without changing the unknown name', async () => {
		const composer = mountComposer();
		expect(composer.chip()?.getAttribute('aria-label')).toBe('Context for A');

		setContextUsage('thread-a', measured('thread-a', 70) as never);
		flushSync();
		expect(composer.donut()).toBe('7%');
		expect(composer.chip()?.querySelector('.ctx-pct')?.textContent).toBe('~7%');
		const measuredMarker = composer.chip()?.querySelector<HTMLElement>('.ctx-estimate-marker');
		expect(measuredMarker?.textContent).toBe('~');
		expect(measuredMarker?.classList.contains('estimated')).toBe(false);
		expect(composer.chip()?.getAttribute('aria-label')).toBe('Context for A: 7% used');

		previewTotal = 70;
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~7%'));
		expect(composer.chip()?.querySelector('.ctx-pct')?.textContent).toBe('~7%');
		const estimatedMarker = composer.chip()?.querySelector<HTMLElement>('.ctx-pct .ctx-estimate-marker');
		expect(estimatedMarker?.textContent).toBe('~');
		expect(estimatedMarker?.classList.contains('estimated')).toBe(true);
		expect(composer.chip()?.getAttribute('aria-label')).toBe('Context for A: estimated 7%');
	});
});

describe('turns after a preview (trace rows 5, 7)', () => {
	it('shows the receipt immediately on first open while the preview loads', () => {
		fetchMock.mockImplementationOnce(() => new Promise(() => {}));
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		expect(composer.donut()).toBe('42%');
		expect(composer.headline()).toContain('42%');
	});

	it('does not carry a preview across a model switch while refreshing', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		let finish!: (response: unknown) => void;
		fetchMock.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
		recordMeasuredUsage('thread-a', measured('thread-a', 510) as never);
		flushSync();
		expect(composer.donut()).toBe('~30%');
		activeModelId.set('model-b');
		flushSync();
		expect(composer.donut()).not.toBe('~30%');
		finish({ ok: true, json: async () => ({ preview: measured('thread-a', 700) }) });
		await vi.waitFor(() => expect(previewCalls()).toHaveLength(3));
	});

	it('keeps the carried reading when the sheet closes during refresh until the read lands', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		let finish!: (response: unknown) => void;
		fetchMock.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
		recordMeasuredUsage('thread-a', measured('thread-a', 510) as never);
		flushSync();
		expect(composer.donut()).toBe('~30%');
		composer.props.sheetView = null;
		flushSync();
		expect(composer.donut()).toBe('~30%');
		finish({ ok: true, json: async () => ({ preview: {
			...measured('thread-a', 640), token_source: 'estimated', token_usage_available: false, preview: true,
		} }) });
		await vi.waitFor(() => expect(composer.donut()).toBe('~64%'));
	});

	it('shows the newest receipt on reopening after a closed turn', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		composer.props.sheetView = null;
		flushSync();
		recordMeasuredUsage('thread-a', measured('thread-a', 800) as never);
		flushSync();
		fetchMock.mockImplementationOnce(() => new Promise(() => {}));
		composer.props.sheetView = 'context';
		flushSync();
		expect(composer.donut()).toBe('80%');
		expect(composer.headline()).toContain('80%');
		expect(previewCalls()).toHaveLength(2);
	});

	it('returns to the receipt and shows the error when refresh fails', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		let fail!: (error: Error) => void;
		fetchMock.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
		recordMeasuredUsage('thread-a', measured('thread-a', 510) as never);
		flushSync();
		expect(composer.donut()).toBe('~30%');
		fail(new Error('Preview failed'));
		await vi.waitFor(() => expect(composer.donut()).toBe('51%'));
		expect(host.textContent).toContain('Could not refresh');
	});

	it('keeps the preview in the open sheet and donut until the next preview lands', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));

		let finish!: (response: unknown) => void;
		fetchMock.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
		recordMeasuredUsage('thread-a', measured('thread-a', 510) as never);
		flushSync();
		expect(composer.donut()).toBe('~30%');
		expect(composer.headline()).toContain('30%');
		expect(host.textContent).toContain('Updating');

		finish({ ok: true, json: async () => ({ preview: {
			...measured('thread-a', 640), token_source: 'estimated', token_usage_available: false, preview: true,
		} }) });
		await vi.waitFor(() => expect(composer.donut()).toBe('~64%'));
		expect(composer.headline()).toContain('64%');
	});

	it('shows the measured reading once a turn completes after a preview', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		composer.props.sheetView = null;
		flushSync();

		recordMeasuredUsage('thread-a', measured('thread-a', 510) as never);
		flushSync();

		expect(composer.donut()).toBe('51%');
	});

	it('keeps the reading when a completed turn could not measure', () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		flushSync();

		recordMeasuredUsage('thread-a', {
			...measured('thread-a', 0), total_used: null, token_usage_available: false,
		} as never);
		flushSync();

		expect(composer.donut()).toBe('42%');
	});
});

describe('a reading kept past an unmeasurable turn', () => {
	it('refreshes an old preview after an unmeasured turn while closed', async () => {
		const composer = mountComposer();
		beginThreadEntry('thread-a');
		adoptThreadSnapshot('thread-a', [receiptRow('thread-a', 'm1', measured('thread-a', 420))] as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		composer.props.sheetView = null;
		flushSync();
		previewTotal = 640;
		recordMeasuredUsage('thread-a', { ...measured('thread-a', 0), total_used: null, token_usage_available: false } as never);
		flushSync();
		await vi.waitFor(() => expect(composer.donut()).toBe('~64%'));
	});

	it('stays on screen, then a fresh estimate replaces it', async () => {
		const composer = mountComposer();
		beginThreadEntry('thread-a');
		adoptThreadSnapshot('thread-a', [receiptRow('thread-a', 'm1', measured('thread-a', 420))] as never);
		flushSync();
		expect(composer.donut()).toBe('42%');
		expect(previewCalls()).toHaveLength(0);

		recordMeasuredUsage('thread-a', {
			...measured('thread-a', 0), total_used: null, token_usage_available: false,
		} as never);
		flushSync();
		expect(composer.donut()).toBe('42%');

		await vi.waitFor(() => expect(composer.donut()).toBe('~30%'));
		expect(previewCalls()).toHaveLength(1);
	});
});

describe('model change (trace row 10)', () => {
	it('retires the preview and reads it again with the new model', async () => {
		const composer = mountComposer();
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		composer.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(composer.headline()).toContain('30%'));

		previewTotal = 640;
		activeModelId.set('model-b');
		flushSync();

		await vi.waitFor(() => expect(composer.headline()).toContain('64%'));
		expect(composer.donut()).toBe('~64%');
		expect(previewCalls()).toHaveLength(2);
		expect(previewCalls()[1]).toContain('model=model-b');
	});
});

describe('leaf composer (trace row 13)', () => {
	it("reads only its own thread's reading and preview", async () => {
		subagentMeta.set(new Map([['thread-b', { name: 'Worker' } as never]]));
		const main = mountComposer();
		const leaf = mountComposer('thread-b');
		setContextUsage('thread-a', measured('thread-a', 420) as never);
		setContextUsage('thread-b', measured('thread-b', 110) as never);
		flushSync();

		main.props.sheetView = 'context';
		flushSync();
		await vi.waitFor(() => expect(main.donut()).toBe('~30%'));

		expect(leaf.donut()).toBe('11%');
		expect(previewCalls().every(url => url.includes('/api/threads/thread-a/'))).toBe(true);
	});
});
