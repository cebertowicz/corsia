import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const wsMocks = vi.hoisted(() => ({
	listeners: new Map<string, (data: unknown) => void>(),
	subscribeThread: vi.fn(),
	unsubscribeThread: vi.fn(),
	sendMessage: vi.fn(() => 'input-1'),
	stopTurn: vi.fn(),
}));

vi.mock('$lib/ws', () => ({
	on: (event: string, listener: (data: unknown) => void) => {
		wsMocks.listeners.set(event, listener);
		return () => wsMocks.listeners.delete(event);
	},
	subscribeThread: wsMocks.subscribeThread,
	unsubscribeThread: wsMocks.unsubscribeThread,
	sendMessage: wsMocks.sendMessage,
	stopTurn: wsMocks.stopTurn,
}));

// The leaf's own reads and listeners are under test; the transcript's update cards are not.
vi.mock('$lib/stores/threadUpdateCards', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/threadUpdateCards')>()),
	loadUpdateCards: vi.fn(async () => {}),
	watchUpdateCardNudges: vi.fn(),
}));

import SubagentConversationLeaf from './SubagentConversationLeaf.svelte';
import { token } from '$lib/stores/auth';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '$lib/stores/threads';
import { isDisplayedThread } from '$lib/stores/displayedThreads';
import { activeModelId, models } from '$lib/stores/models';
import { connectionState } from '$lib/stores/connection';
import { coreSettings } from '$lib/stores/settings';
import { withOpenTime } from '$lib/__tests__/fixtures/openTimeServer';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function response(messages: unknown[] = [], turn_summaries: Record<string, unknown> = {}) {
	return {
		ok: true,
		status: 200,
		json: async () => ({ messages, turn_summaries, has_more: false }),
	};
}

// The leaf's composer reads a next-turn estimate on entry when the child has no
// stored reading, and the leaf reads the worker's own model and effort from its
// status and its own catalog; these tests count only the leaf's own history reads.
const PREVIEW_PATH = '/context-snapshot/next-turn';
const STATUS_PATH = '/api/delegation/threads/';
const CATALOG_PATH = '/api/models';

function isHistoryRead(url: unknown): boolean {
	return ![PREVIEW_PATH, STATUS_PATH, CATALOG_PATH, '/api/open-time'].some((path) => String(url).includes(path));
}

function historyCalls(mock: unknown): number {
	return (mock as ReturnType<typeof vi.fn>).mock.calls.filter(([url]) => isHistoryRead(url)).length;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-bearer');
	wsMocks.listeners.clear();
	wsMocks.subscribeThread.mockReset();
	wsMocks.unsubscribeThread.mockReset();
	wsMocks.sendMessage.mockClear();
	wsMocks.sendMessage.mockReturnValue('input-1');
	activeModelId.set('network/model');
	models.set([{
		id: 'network/model', name: 'Network Model', size: '', backend: 'network', speed: '', status: 'ready',
	}]);
	connectionState.set('connected');
	coreSettings.set(null);
	vi.stubGlobal('fetch', vi.fn(async () => response()));
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove();
	token.set(null);
	activeThreadId.set(null);
	messages.set([]);
	activeModelId.set(null);
	models.set([]);
	connectionState.set('disconnected');
	coreSettings.set(null);
	vi.unstubAllGlobals();
});

describe('SubagentConversationLeaf', () => {
	it('shows a retry message when the agent chat fails to load', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500 })));
		component = mount(SubagentConversationLeaf, { target: host, props: { threadId: 'child-error' } });
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('This agent chat couldn’t load. Try again.'));
		expect(host.querySelector('[aria-label="Agent chat"]')).not.toBeNull();
	});

	it('claims the child, loads it into the shared store once, and reloads on reconnect', async () => {
		activeThreadId.set('parent-thread');
		vi.stubGlobal('fetch', withOpenTime(vi.fn(async () => response([{
			id: 'child-row', thread_id: 'child-1', role: 'assistant', content: 'child says hi',
			created_at: '2026-09-23T10:00:00Z',
		}])) as unknown as typeof fetch));
		component = mount(SubagentConversationLeaf, {
			target: host,
			props: { threadId: 'child-1' },
		});
		flushSync();

		await vi.waitFor(() => {
			expect(wsMocks.subscribeThread).toHaveBeenCalledWith('child-1');
			expect(get(messages).map((m) => m.id)).toEqual(['child-row']);
		});
		expect(isDisplayedThread('child-1')).toBe(true);
		expect(historyCalls(fetch)).toBe(1);
		// No stored reading on the child's first page, so its donut asks for one estimate.
		await vi.waitFor(() => expect(
			vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes(PREVIEW_PATH)),
		).toHaveLength(1));

		wsMocks.listeners.get('connected')?.({});
		expect(wsMocks.subscribeThread).toHaveBeenLastCalledWith('child-1');
		await vi.waitFor(() => expect(historyCalls(fetch)).toBe(2));
		expect(get(messages)).toHaveLength(1);

		await unmount(component);
		component = null;
		expect(isDisplayedThread('child-1')).toBe(false);
		expect(get(messages)).toEqual([]);
	});

	it('passes completed-turn summaries to its Focus transcript', async () => {
		coreSettings.set({ features: { focus_mode: true } } as never);
		const summaries = {
			'turn-summary': {
				started_at: '2026-09-30T12:00:00Z', ended_at: '2026-09-30T12:04:00Z',
				status: 'completed', activity_count: 1,
			},
		};
		vi.stubGlobal('fetch', withOpenTime(vi.fn(async () => response([{
			id: 'child-work', thread_id: 'child-summary', role: 'assistant', content: '',
			metadata: { turn_id: 'turn-summary', tool_calls: [{ id: 'call-1' }] },
			created_at: '2026-09-30T12:00:00Z', activity_weight: 1,
		}], summaries)) as unknown as typeof fetch, () => summaries));
		component = mount(SubagentConversationLeaf, {
			target: host,
			props: { threadId: 'child-summary' },
		});
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('Worked · 1 step · 4 min'));
	});

	it('reloads on activity, terminal, and visibility recovery', async () => {
		activeThreadId.set('parent-thread');
		component = mount(SubagentConversationLeaf, {
			target: host,
			props: { threadId: 'child-recovery' },
		});
		flushSync();
		await vi.waitFor(() => expect(historyCalls(fetch)).toBe(1));

		wsMocks.listeners.get('turn_activity')?.({ thread_id: 'child-recovery' });
		await vi.waitFor(() => expect(historyCalls(fetch)).toBe(2));
		wsMocks.listeners.get('turn_complete')?.({
			thread_id: 'child-recovery', turn_id: 'turn-1', turn_order: 1,
		});
		await vi.waitFor(() => expect(historyCalls(fetch)).toBe(3));
		wsMocks.listeners.get('error')?.({
			thread_id: 'child-recovery', turn_id: 'turn-1', error_type: 'stop_failed',
		});
		await Promise.resolve();
		expect(historyCalls(fetch)).toBe(3);

		const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
		document.dispatchEvent(new Event('visibilitychange'));
		await vi.waitFor(() => expect(historyCalls(fetch)).toBe(4));
		visibility.mockRestore();
	});

	it('keeps unmarked rows visible when a terminal refresh fails', async () => {
		activeThreadId.set('parent-thread');
		const history = vi.fn()
			.mockResolvedValueOnce(response([{
				id: 'running-row', thread_id: 'child-fail-open', role: 'assistant',
				content: 'Still visible', created_at: '2026-09-23T10:00:00Z',
			}]))
			.mockRejectedValueOnce(new Error('offline'));
		const fetchMock = vi.fn(async (url: string) => (
			isHistoryRead(url) ? history(url) : response()
		));
		vi.stubGlobal('fetch', withOpenTime(fetchMock as unknown as typeof fetch));
		component = mount(SubagentConversationLeaf, {
			target: host,
			props: { threadId: 'child-fail-open' },
		});
		flushSync();
		await vi.waitFor(() => expect(get(messages).map((m) => m.id)).toEqual(['running-row']));

		wsMocks.listeners.get('turn_complete')?.({
			thread_id: 'child-fail-open', turn_id: 'turn-1', turn_order: 1,
		});
		await vi.waitFor(() => expect(history).toHaveBeenCalledTimes(2));

		const [row] = get(messages) as Array<Record<string, unknown>>;
		expect(row.id).toBe('running-row');
		expect(host.textContent).toContain('Still visible');
	});

	it('mounts InputBar bound to the subagent thread, not the active thread', async () => {
		activeThreadId.set('parent-thread');
		component = mount(SubagentConversationLeaf, {
			target: host,
			props: { threadId: 'child-3' },
		});
		flushSync();
		expect(host.querySelector('.input-floater')).not.toBeNull();

		const composer = host.querySelector<HTMLElement>('.composer')!;
		composer.textContent = 'hello from the leaf';
		composer.dispatchEvent(new InputEvent('input', { bubbles: true }));
		flushSync();
		const sendBtn = host.querySelector<HTMLButtonElement>('.send-btn')!;
		expect(sendBtn.disabled).toBe(false);
		sendBtn.click();

		await vi.waitFor(() => expect(wsMocks.sendMessage).toHaveBeenCalledOnce());
		// The send carries the worker's own model (none loaded here), never the open thread's.
		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'child-3', 'hello from the leaf', undefined, [], undefined,
		);
	});

	it('unsubscribes the child when the artifact leaf unmounts', async () => {
		component = mount(SubagentConversationLeaf, {
			target: host,
			props: { threadId: 'child-2' },
		});
		flushSync();
		await vi.waitFor(() => expect(wsMocks.subscribeThread).toHaveBeenCalledWith('child-2'));

		await unmount(component);
		component = null;

		expect(wsMocks.unsubscribeThread).toHaveBeenCalledWith('child-2');
		expect(wsMocks.listeners.size).toBe(0);
	});
});
