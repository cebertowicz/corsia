import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import {
	activeThreadId,
	activeToolsByThread,
	messages,
} from '$lib/stores/threads';
import MessageList from './MessageList.svelte';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { coreSettings } from '$lib/stores/settings';
import {
	_resetCheckpointsForTests,
} from '$lib/stores/checkpoints';

const THREAD_ID = 'routed-event-session';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeAll(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
			commitStyles: vi.fn(),
			onfinish: null,
			playState: 'finished',
		})) as unknown as Element['animate'];
	}
});

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	activeToolsByThread.set(new Map());
	coreSettings.set({ features: { focus_mode: false } } as any);
	messages.set([
		{
			id: 'event-input-1',
			thread_id: THREAD_ID,
			role: 'user',
			content: '[EVENT: webhook.github.pr_opened]\nSummary: raw payload should stay hidden',
			created_at: '2026-06-17T00:00:00',
			metadata: { routed_event: 'webhook.github.pr_opened' },
		},
		{
			id: 'event-chip-1',
			thread_id: THREAD_ID,
			role: 'system',
			content: 'cebertowicz/corsia#482 opened: fix spawn visibility',
			created_at: '2026-06-17T00:00:01',
			metadata: {
				type: 'context_event',
				event_type: 'webhook.github.pr_opened',
				summary: 'cebertowicz/corsia#482 opened: fix spawn visibility',
				url: 'https://github.com/cebertowicz/corsia/pull/482',
			},
		},
	]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	messages.set([]);
	coreSettings.set(null);
	_resetCheckpointsForTests();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

function renderList() {
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

describe('MessageList routed event display', () => {
	it('hides raw routed-event input rows and shows the event chip', () => {
		renderList();

		expect(host.textContent).toContain('cebertowicz/corsia#482 opened');
		expect(host.textContent).not.toContain('[EVENT: webhook.github.pr_opened]');
		expect(host.querySelector('.user-bubble')).toBeNull();
		expect(host.querySelector('.ev-chip')).not.toBeNull();
	});

	it('keeps routed events as lines inside the Focus work block', () => {
		coreSettings.set({ features: { focus_mode: true } } as any);
		renderList();

		expect(host.querySelector('.transcript-fold')).not.toBeNull();
		expect(host.querySelector('.ev-chip')).toBeNull();
		expect(host.querySelector('[data-block-line]')).toBeNull();
		host.querySelector<HTMLButtonElement>('.fold-toggle')?.click();
		flushSync();
		// B19: a delivered context update is a quiet line at its time, inside the block.
		expect(host.querySelector('.transcript-fold [data-block-line="context_update"]')?.textContent).toContain('cebertowicz/corsia#482 opened');
	});

	it('keeps assistant output produced by a routed-event turn visible', () => {
		messages.set([
			{
				id: 'event-input-1',
				thread_id: THREAD_ID,
				role: 'user',
				content: '[EVENT: host_access.activated]\nSummary: raw payload should stay hidden',
				created_at: '2026-06-17T00:00:00',
				metadata: { routed_event: 'host_access.activated' },
			},
			{
				id: 'event-result-1',
				thread_id: THREAD_ID,
				role: 'assistant',
				content: 'The reviewed host action completed successfully.',
				created_at: '2026-06-17T00:00:01',
				metadata: { routed_event: 'host_access.activated' },
			},
		]);

		renderList();

		expect(host.textContent).not.toContain('[EVENT: host_access.activated]');
		expect(host.textContent).toContain('The reviewed host action completed successfully.');
	});

	it('collapses noisy passive context events into one digest row', () => {
		messages.set([
			{
				id: 'ctx-1',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'AGENTS.md updated',
				created_at: '2026-06-17T00:00:00',
				metadata: {
					type: 'context_event',
					event_type: 'context.source_changed',
					source_label: 'AGENTS.md',
				},
			},
			{
				id: 'ctx-2',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'PROJECT.md updated',
				created_at: '2026-06-17T00:00:01',
				metadata: {
					type: 'context_event',
					event_type: 'context.source_changed',
					source_label: 'PROJECT.md',
				},
			},
			{
				id: 'ctx-3',
				thread_id: THREAD_ID,
				role: 'system',
				content: '2 artifacts added',
				created_at: '2026-06-17T00:00:02',
				metadata: {
					type: 'context_event',
					event_type: 'context.digest',
					artifact_count_delta: 2,
				},
			},
		]);

		renderList();

		expect(host.textContent).toContain('2 context sources updated, 2 artifacts added');
		expect(host.textContent).not.toContain('AGENTS.md updated');
		expect(host.querySelectorAll('.message.system')).toHaveLength(1);
		expect(host.querySelector('.ev-chip')?.tagName).toBe('DIV');
		expect(host.querySelector('.context-event-chevron')).toBeNull();
	});

	it('renders CORSIA context boundaries as short passive rows', () => {
		messages.set([
			{
				id: 'ctx-boundary',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Context compacted for this turn (36967/200000 tokens)',
				created_at: '2026-06-17T00:00:00',
				metadata: {
					type: 'context_event',
					event_type: 'context.compacted',
					trigger_reason: 'harness_model_switch',
					continuity_mode: 'compacted',
					total_tokens: 36_967,
					model_context_window: 200_000,
				},
			},
		]);

		renderList();

		expect(host.textContent).toContain('Context boundary · 37k/200k');
		expect(host.textContent).not.toContain('Context compacted for this turn');
		expect(host.querySelector('.ev-chip')?.tagName).toBe('DIV');
		expect(host.querySelector('.context-event-chevron')).toBeNull();
	});

	it('renders provider compaction as a passive context event', () => {
		messages.set([
			{
				id: 'ctx-provider-compact',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Codex compacted its provider-local context.',
				created_at: '2026-06-17T00:00:00',
				metadata: {
					type: 'context_event',
					event_type: 'provider.context_compacted',
					provider: 'codex',
					continuity_mode: 'provider_summary',
					repair_owner: 'ctx-stability',
				},
			},
		]);

		renderList();

		expect(host.textContent).toContain('Codex compacted its provider-local context.');
		expect(host.querySelector('.ev-chip')?.tagName).toBe('DIV');
	});

	it('renders provider compaction pending as a passive context event', () => {
		messages.set([
			{
				id: 'ctx-provider-compact-pending',
				thread_id: THREAD_ID,
				role: 'system',
				content: 'Claude Code is about to compact its provider-local context.',
				created_at: '2026-06-17T00:00:00',
				metadata: {
					type: 'context_event',
					event_type: 'provider.context_compaction_pending',
					provider: 'claude-code',
					continuity_mode: 'provider_pending',
					repair_owner: 'ctx-stability',
				},
			},
		]);

		renderList();

		expect(host.textContent).toContain('Claude Code is about to compact its provider-local context.');
		expect(host.querySelector('.ev-chip')?.tagName).toBe('DIV');
	});

	it('renders external harness assistant footers with concise provider/model identity', () => {
		messages.set([
			{
				id: 'codex-assistant',
				thread_id: THREAD_ID,
				role: 'assistant',
				content: 'Done.',
				created_at: '2026-06-17T00:00:00',
				metadata: {
					harness: 'codex',
					model: 'codex/gpt-5-codex',
					model_name: 'GPT-5 Codex',
					context_usage: { snapshot_id: 'ctx-codex', model_context_window: 200000 },
				},
			},
			{
				id: 'cc-assistant',
				thread_id: THREAD_ID,
				role: 'assistant',
				content: 'Also done.',
				created_at: '2026-06-17T00:00:01',
				metadata: {
					harness: 'claude-code',
					model: 'claude-code/sonnet',
					model_name: 'Sonnet',
					context_usage: { snapshot_id: 'ctx-cc', model_context_window: 1000000 },
				},
			},
		]);

		renderList();

		expect(host.textContent).toContain('Codex · GPT-5 Codex');
		expect(host.textContent).toContain('Claude Code · Sonnet');
		expect(host.textContent).not.toContain('☁️');
		expect(host.textContent).not.toContain('🖥️');
	});

	it('renders native model footers without icon or machine namespace leakage', () => {
		messages.set([
			{
				id: 'native-assistant',
				thread_id: THREAD_ID,
				role: 'assistant',
				content: 'Native done.',
				created_at: '2026-06-17T00:00:00',
				metadata: {
					harness: 'native',
					model: 'omlx@ns-test/Model-9B-mlx-mxfp8',
					context_usage: { snapshot_id: 'ctx-native', model_context_window: 262144 },
				},
			},
		]);

		renderList();

		expect(host.textContent).toContain('oMLX · Model-9B-mlx-mxfp8');
		expect(host.textContent).not.toContain('ns-test');
		expect(host.textContent).not.toContain('🖥️');
	});
});
