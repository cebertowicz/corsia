import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { token } from '$lib/stores/auth';
import { coreSettings } from '$lib/stores/settings';
import MessageList from './MessageList.svelte';

// These tests count tool-result fetches; the transcript's update-card read is not one.
vi.mock('$lib/stores/threadUpdateCards', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/threadUpdateCards')>()),
	loadUpdateCards: vi.fn(async () => {}),
}));

const THREAD_ID = 'tool-activity-session';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function toolStep(id: string, turnId: string | undefined, command: string, content = ''): any {
	return {
		id,
		thread_id: THREAD_ID,
		role: 'assistant',
		content,
		created_at: '2026-07-29T12:00:00Z',
		metadata: {
			...(turnId ? { turn_id: turnId } : {}),
			tool_calls: [{ name: 'mcp__corsia__bash', args: { command } }],
		},
	};
}

function assistantTextStep(id: string, turnId: string, content: string): any {
	return {
		id,
		thread_id: THREAD_ID,
		role: 'assistant',
		content,
		created_at: '2026-07-29T12:00:00Z',
		metadata: { turn_id: turnId },
	};
}

function renderList(threadId?: string): void {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
			threadId,
		},
	});
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	activeToolsByThread.set(new Map());
	token.set('test-token');
	messages.set([]);
	coreSettings.set({ features: { focus_mode: false } } as any);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	token.set(null);
	messages.set([]);
	coreSettings.set(null);
	vi.unstubAllGlobals();
});

describe('MessageList persisted tool activity', () => {
	it('collapses three sequential tool-only assistant steps from one turn into one progressive group', () => {
		messages.set([
			toolStep('tool-1', 'turn-1', 'git status'),
			toolStep('tool-2', 'turn-1', 'git diff --stat'),
			toolStep('tool-3', 'turn-1', 'git log -1'),
		]);

		renderList();

		expect(host.querySelectorAll('.ta-summary')).toHaveLength(1);
		expect(host.querySelector('.ta-summary')?.textContent).toContain('Ran 3 commands');
	});

	it('loads a tool result only when its row expands', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({ result: 'lazy output' }),
		});
		vi.stubGlobal('fetch', fetchMock);
		messages.set([{
			...toolStep('tool-1', 'turn-1', 'git status'),
			metadata: { tool_calls: [{ id: 'call-1', name: 'mcp__corsia__bash', args: { command: 'git status' }, result_state: 'deferred' }] },
		}]);

		renderList();

		expect(fetchMock).not.toHaveBeenCalled();
		host.querySelector<HTMLButtonElement>('.ta-summary')!.click();
		flushSync();
		expect(fetchMock).not.toHaveBeenCalled();
		host.querySelector<HTMLButtonElement>('.ta-row')!.click();
		await Promise.resolve();
		await Promise.resolve();
		flushSync();

		expect(fetchMock).toHaveBeenCalledWith(
			`/api/threads/${THREAD_ID}/messages/tool-1/tool-results/call-1?token=test-token`,
		);
		await vi.waitFor(() => expect(host.textContent).toContain('lazy output'));
	});

	it('shows a retryable error instead of an empty result when detail loading fails', async () => {
		const fetchMock = vi.fn().mockRejectedValue(new Error('offline'));
		vi.stubGlobal('fetch', fetchMock);
		messages.set([{
			...toolStep('tool-1', 'turn-1', 'git status'),
			metadata: { tool_calls: [{ id: 'call-1', name: 'mcp__corsia__bash', args: { command: 'git status' }, result_state: 'deferred' }] },
		}]);

		renderList();
		host.querySelector<HTMLButtonElement>('.ta-summary')!.click();
		flushSync();
		host.querySelector<HTMLButtonElement>('.ta-row')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Could not load output.'));
		host.querySelector<HTMLButtonElement>('.ta-retry')!.click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
	});

	it('shows loading while a deferred result is in flight', async () => {
		let resolveFetch: ((value: unknown) => void) | undefined;
		vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise((resolve) => {
			resolveFetch = resolve;
		})));
		messages.set([{
			...toolStep('tool-1', 'turn-1', 'git status'),
			metadata: { tool_calls: [{ id: 'call-1', name: 'mcp__corsia__bash', args: { command: 'git status' }, result_state: 'deferred' }] },
		}]);

		renderList();
		host.querySelector<HTMLButtonElement>('.ta-summary')!.click();
		flushSync();
		host.querySelector<HTMLButtonElement>('.ta-row')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Loading output…'));
		resolveFetch?.({ ok: true, json: async () => ({ result: 'lazy output' }) });
		await vi.waitFor(() => expect(host.textContent).toContain('lazy output'));
	});

	it('shows absent output without a detail request', () => {
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
		messages.set([{
			...toolStep('tool-1', 'turn-1', 'git status'),
			metadata: { tool_calls: [{ id: 'call-1', name: 'mcp__corsia__bash', args: { command: 'git status' }, result_state: 'absent' }] },
		}]);

		renderList();
		host.querySelector<HTMLButtonElement>('.ta-summary')!.click();
		flushSync();
		host.querySelector<HTMLButtonElement>('.ta-row')!.click();
		flushSync();

		expect(fetchMock).not.toHaveBeenCalled();
		expect(host.textContent).toContain('No output.');
	});

	it('renders and loads tool output for its own thread when bound to a non-active thread (#1949)', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({ result: 'leaf output' }),
		});
		vi.stubGlobal('fetch', fetchMock);
		messages.set([
			toolStep('active-tool', 'turn-1', 'git status'),
			{
				...toolStep('leaf-tool', 'turn-9', 'ls'),
				thread_id: 'leaf-thread',
				metadata: { tool_calls: [{ id: 'call-9', name: 'mcp__corsia__bash', args: { command: 'ls' }, result_state: 'deferred' }] },
			},
		]);

		renderList('leaf-thread');

		expect(host.querySelectorAll('.ta-summary')).toHaveLength(1);
		host.querySelector<HTMLButtonElement>('.ta-summary')!.click();
		flushSync();
		host.querySelector<HTMLButtonElement>('.ta-row')!.click();
		await Promise.resolve();
		await Promise.resolve();
		flushSync();

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(String(fetchMock.mock.calls[0][0])).toContain('/api/threads/leaf-thread/messages/leaf-tool/tool-results/call-9');
		await vi.waitFor(() => expect(host.textContent).toContain('leaf output'));
	});

	it('uses actual assistant text as a boundary between otherwise matching tool-only steps', () => {
		messages.set([
			toolStep('tool-1', 'turn-1', 'git status'),
			toolStep('tool-2', 'turn-1', 'git diff --stat'),
			assistantTextStep('assistant-text', 'turn-1', 'I found the issue.'),
			toolStep('tool-3', 'turn-1', 'git log -1'),
		]);

		renderList();

		expect(host.querySelectorAll('.ta-summary')).toHaveLength(2);
		expect(host.querySelector('[data-message-id="assistant-text"]')?.textContent)
			.toContain('I found the issue.');
	});

	it('renders hydrated tool sets inside their owning assistant messages', () => {
		messages.set([
			toolStep('assistant-a', 'turn-1', 'cat A.md', 'Assistant A'),
			toolStep('assistant-b', 'turn-1', 'cat B.md', 'Assistant B'),
			assistantTextStep('assistant-c', 'turn-1', 'Assistant C'),
		]);

		renderList();

		const assistantA = host.querySelector('[data-message-id="assistant-a"]');
		const assistantB = host.querySelector('[data-message-id="assistant-b"]');
		const assistantC = host.querySelector('[data-message-id="assistant-c"]');
		expect(assistantA?.textContent).toContain('Assistant A');
		expect(assistantA?.querySelectorAll('.ta-summary')).toHaveLength(1);
		expect(assistantB?.textContent).toContain('Assistant B');
		expect(assistantB?.querySelectorAll('.ta-summary')).toHaveLength(1);
		expect(assistantC?.textContent).toContain('Assistant C');
		expect(assistantC?.querySelector('.ta-summary')).toBeNull();
	});

	it('leaves tool-only assistant rows without a turn identifier as separate provider batches', () => {
		messages.set([
			toolStep('tool-1', undefined, 'git status'),
			toolStep('tool-2', undefined, 'git diff --stat'),
			toolStep('tool-3', undefined, 'git log -1'),
		]);

		renderList();

		expect(host.querySelectorAll('.ta-summary')).toHaveLength(3);
	});
});
