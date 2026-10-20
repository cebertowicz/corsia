import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { coreSettings } from '$lib/stores/settings';
import {
	activeThreadId,
	activeToolsByThread,
	messages,
	setThreadContent,
	streamContentByThread,
} from '$lib/stores/threads';
import { _resetTurnStateForTests, applyLifecycle, applyTerminal } from '$lib/ws/turnState';
import { registerSubagent, subagentMeta } from '$lib/stores/subagents';
import { setWs, subscribedThreads } from '$lib/ws/state';
import MessageList from './MessageList.svelte';

const THREAD_ID = 'focus-running';
const STREAMED_TEXT = 'Current streamed work';

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
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	subagentMeta.set(new Map());
	streamContentByThread.set(new Map());
	messages.set([]);
	subscribedThreads.clear();
	setWs(null);
	coreSettings.set({ features: { focus_mode: true } } as any);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	subagentMeta.set(new Map());
	streamContentByThread.set(new Map());
	messages.set([]);
	subscribedThreads.clear();
	setWs(null);
	coreSettings.set(null);
});

describe('Focus mode while a Turn is running', () => {
	it('folds persisted and streaming ordinary work without any reply inference', () => {
		messages.set([{
			id: 'running-step', thread_id: THREAD_ID, role: 'assistant',
			content: 'Persisted running work', created_at: '2026-09-27T20:00:00Z',
			metadata: { turn_id: 'turn-live', tool_calls: [{ id: 'tool-1' }] },
		}]);
		setThreadContent(THREAD_ID, STREAMED_TEXT);
		activeToolsByThread.set(new Map([[THREAD_ID, [{ id: 'live-tool', name: 'read_file', phrase: 'Reading source', status: 'running' }]]]) as any);
		applyLifecycle(
			THREAD_ID,
			{ turnId: 'turn-live', turnOrder: 1 },
			undefined,
			Date.now() - 65_000,
		);
		renderList();

		expect(host.textContent).not.toContain('Persisted running work');
		expect(host.textContent).not.toContain(STREAMED_TEXT);
		expect(host.querySelector('[data-message-id="running-step"]')).toBeNull();
		expect(host.querySelector('.tool-activity')).toBeNull();
		expect(host.querySelectorAll('.transcript-fold')).toHaveLength(1);
		const liveLabel = 'Working · Reading source';
		const liveToggle = host.querySelector<HTMLButtonElement>('.live-work .fold-toggle')!;
		expect(host.textContent).toContain(liveLabel);
		expect(host.querySelector('.live-work .live-step')?.getAttribute('title')).toBe(liveLabel);
		expect(liveToggle.getAttribute('aria-label')).toBe('Expand current work: Reading source');
		liveToggle.click();
		flushSync();
		expect(host.textContent).toContain('Persisted running work');
		expect(host.querySelector('.tool-activity')).not.toBeNull();
	});

	it('subscribes to an active descendant for its title and tool state, then releases it on completion', async () => {
		const sent: Array<{ type: string; thread_id: string }> = [];
		setWs({ readyState: WebSocket.OPEN, send: (raw: string) => sent.push(JSON.parse(raw)) } as unknown as WebSocket);
		applyLifecycle('child', { turnId: 'child-turn', turnOrder: 1 }, THREAD_ID, Date.now() - 65_000);
		renderList();

		expect(sent).toContainEqual({ type: 'subscribe_session', thread_id: 'child' });
		expect(host.querySelector('.live-spinner')?.classList.contains('subagent-only')).toBe(true);
		expect(host.querySelector('.live-step')?.textContent).toBe('Working');

		registerSubagent(THREAD_ID, 'child', 'UI worker');
		activeToolsByThread.set(new Map([['child', [{ id: 'child-tool', name: 'read_file', phrase: 'Checking UI', status: 'running' }]]]) as any);
		flushSync();
		// The new phrase waits out the shown one's minimum hold before it replaces it.
		await vi.waitFor(() => expect(host.querySelector('.live-step')?.textContent).toBe('Working · UI worker: Checking UI'), { timeout: 3000 });
		expect(host.querySelector('.live-work .fold-toggle')?.getAttribute('aria-label')).toBe('Expand current work: UI worker: Checking UI');

		applyTerminal('child', { turnId: 'child-turn', turnOrder: 1 });
		flushSync();
		expect(sent).toContainEqual({ type: 'unsubscribe_session', thread_id: 'child' });
	});

	it('holds the actor with its phrase when a newer descendant takes the preview', async () => {
		applyLifecycle('older', { turnId: 'older-turn', turnOrder: 1 }, THREAD_ID, Date.now() - 65_000);
		registerSubagent(THREAD_ID, 'older', 'Older worker');
		registerSubagent(THREAD_ID, 'newer', 'Newer worker');
		activeToolsByThread.set(new Map([['older', [{ id: 'older-tool', name: 'read_file', phrase: 'Reading old task', status: 'running' }]]]) as any);
		renderList();
		await vi.waitFor(() => expect(host.querySelector('.live-step')?.textContent).toBe('Working · Older worker: Reading old task'), { timeout: 3000 });

		applyLifecycle('newer', { turnId: 'newer-turn', turnOrder: 1 }, THREAD_ID, Date.now());
		activeToolsByThread.update((byThread) => new Map([...byThread, ['newer', [{ id: 'newer-tool', name: 'grep', hint: '', phrase: 'Checking UI', status: 'running' }]]]));
		flushSync();
		// The held phrase keeps its own actor; the name never changes ahead of it.
		expect(host.querySelector('.live-step')?.textContent).toBe('Working · Older worker: Reading old task');
		await vi.waitFor(() => expect(host.querySelector('.live-step')?.textContent).toBe('Working · Newer worker: Checking UI'), { timeout: 3000 });
	});

	it('renders streaming tools plainly with no live fold when Focus is off', () => {
		coreSettings.set({ features: { focus_mode: false } } as any);
		activeToolsByThread.set(new Map([[THREAD_ID, [{ id: 'live-tool', name: 'read_file', phrase: 'Reading source', status: 'running' }]]]) as any);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		renderList();

		expect(host.querySelector('.tool-activity')).not.toBeNull();
		expect(host.querySelector('.live-work')).toBeNull();
		expect(host.querySelector('.fold-toggle')).toBeNull();
	});

	it('keeps live work expanded when its first ordinary row persists', () => {
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		renderList();

		const liveToggle = host.querySelector<HTMLButtonElement>('.live-work .fold-toggle')!;
		liveToggle.click();
		flushSync();
		expect(liveToggle.getAttribute('aria-expanded')).toBe('true');

		messages.set([{
			id: 'first-persisted-work', thread_id: THREAD_ID, role: 'assistant',
			content: 'First persisted work', created_at: '2026-09-30T01:00:00Z',
			metadata: { turn_id: 'turn-live' },
		}]);
		flushSync();

		const persistedToggle = host.querySelector<HTMLButtonElement>('.live-work .fold-toggle')!;
		expect(persistedToggle.getAttribute('aria-expanded')).toBe('true');
		expect(host.textContent).toContain('First persisted work');
	});

	it('shows earlier-Turn work in the one live run, before the running Turn', () => {
		messages.set([{
			id: 'earlier-work', thread_id: THREAD_ID, role: 'assistant',
			content: 'Earlier wake work', created_at: '2026-09-30T01:00:00Z',
			metadata: { turn_id: 'turn-earlier' },
		}]);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 2 });
		renderList();

		expect(host.querySelectorAll('.transcript-fold')).toHaveLength(1);
		const live = host.querySelector<HTMLButtonElement>('.live-work .fold-toggle')!;
		live.click();
		flushSync();
		expect(host.querySelector('.live-work')?.textContent).toContain('Earlier wake work');

		messages.update((rows) => [...rows, {
			id: 'live-work-row', thread_id: THREAD_ID, role: 'assistant',
			content: 'Live Turn work', created_at: '2026-09-30T01:01:00Z',
			metadata: { turn_id: 'turn-live' },
		}]);
		flushSync();
		expect(host.querySelectorAll('.transcript-fold')).toHaveLength(1);
		const liveText = host.querySelector('.live-work')?.textContent ?? '';
		expect(liveText.indexOf('Earlier wake work')).toBeGreaterThan(-1);
		expect(liveText.indexOf('Live Turn work')).toBeGreaterThan(liveText.indexOf('Earlier wake work'));

		applyTerminal(THREAD_ID, { turnId: 'turn-live', turnOrder: 2 });
		flushSync();
		expect(host.querySelector('.live-work')).toBeNull();
		const merged = host.querySelector<HTMLButtonElement>('.transcript-fold .fold-toggle')!;
		expect(merged.getAttribute('aria-expanded')).toBe('true');
		expect(host.textContent).toContain('Live Turn work');
	});

	it('holds a live phrase the way the plan row does', () => {
		activeToolsByThread.set(new Map([[THREAD_ID, [{ id: 'tool-a', name: 'read_file', phrase: 'Reading source', status: 'running' }]]]) as any);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		renderList();
		expect(host.querySelector('.live-work .live-step')?.textContent).toContain('Reading source');

		activeToolsByThread.set(new Map([[THREAD_ID, [{ id: 'tool-b', name: 'grep', phrase: 'Searching code', status: 'running' }]]]) as any);
		flushSync();
		expect(host.querySelector('.live-work .live-step')?.textContent).toContain('Reading source');
	});

	it('folds streamed text for agent-started and queued-input Turns alike', () => {
		messages.set([{
			id: 'queued-send', thread_id: THREAD_ID, role: 'user', content: 'later',
			created_at: '2026-09-27T20:00:01Z', delivery_state: 'queued',
			active_turn_id: null, metadata: { delivery_intent: 'queue_after_turn' },
		}]);
		setThreadContent(THREAD_ID, STREAMED_TEXT);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		renderList();

		expect(host.textContent).not.toContain(STREAMED_TEXT);
		expect(host.querySelector('.transcript-fold')).not.toBeNull();
		expect(host.textContent).toContain('Working');
	});

	it('reads the same live step as the plan row when the tool store empties mid-Turn', () => {
		const startedAt = Date.now() - 65_000;
		activeToolsByThread.set(new Map([[THREAD_ID, [{ id: 'live-tool', name: 'read_file', phrase: 'Reading source', status: 'running' }]]]) as any);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 }, undefined, startedAt);
		renderList();
		expect(host.textContent).toContain('Working · Reading source');

		activeToolsByThread.set(new Map([[THREAD_ID, []]]));
		flushSync();
		const focusLabel = host.querySelector('.live-work .fold-toggle')?.textContent ?? '';
		expect(focusLabel).toContain('Working · Reading source');
		expect(focusLabel).not.toContain('Working Working');
	});

	it('applies Focus classification to imminent delivery rows', () => {
		messages.set([
			{
				id: 'pending-work', thread_id: THREAD_ID, role: 'assistant',
				content: 'Pending ordinary output', reasoning: 'Pending reasoning',
				created_at: '2026-09-27T20:00:00Z', delivery_state: 'queued',
				metadata: { tool_calls: [{ id: 'tool-1' }] },
			},
			{
				id: 'pending-update', thread_id: THREAD_ID, role: 'assistant',
				content: 'Pending user-facing answer', created_at: '2026-09-27T20:00:01Z',
				delivery_state: 'queued', metadata: { for_user: true },
			},
		]);
		applyLifecycle(THREAD_ID, { turnId: 'turn-live', turnOrder: 1 });
		renderList();

		expect(host.querySelector('.pending-input-region')).toBeNull();
		expect(host.querySelector('[data-message-id="pending-work"]')).toBeNull();
		expect(host.querySelector('[data-message-id="pending-update"]')).toBeNull();

		coreSettings.set({ features: { focus_mode: false } } as any);
		flushSync();

		const pending = host.querySelector('.pending-input-region');
		expect(pending?.textContent).toContain('Pending ordinary output');
		expect(pending?.textContent).toContain('Pending user-facing answer');
		expect(pending?.querySelectorAll('[data-message-id="pending-work"]')).toHaveLength(1);
		expect(pending?.querySelectorAll('[data-message-id="pending-update"]')).toHaveLength(1);
	});

	it('folds a finished ordinary assistant step', () => {
		messages.set([{
			id: 'finished-work', thread_id: THREAD_ID, role: 'assistant',
			content: 'Finished background work',
			created_at: '2026-09-27T20:00:00Z', metadata: { turn_id: 'turn-done' },
		}]);
		renderList();

		expect(host.querySelector('.transcript-fold')).not.toBeNull();
		expect(host.querySelector('[data-message-id="finished-work"]')).toBeNull();
	});
});
