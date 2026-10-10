import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPanel from './ChatPanel.svelte';
import { withEmptyAttentionQueue } from '../__tests__/fixtures/attentionQueue';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages, reasoningEffort, threads } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { _resetCheckpointsForTests } from '$lib/stores/checkpoints';
import { _resetSubagentHydrationForTests, subagentMeta } from '$lib/stores/subagents';
import { clearAllContextUsage } from '$lib/stores/contextUsageByThread';

/**
 * Opening a worker thread from a cold page has no live WS frame to lean on.
 * Whether the thread IS a subagent is what routes a send to the child's stdin
 * channel and what keeps Send enabled, so it is read from the server rather
 * than left to whichever frame happens to arrive first.
 */

const originalFetch = globalThis.fetch;
const CHILD_ID = 'child-thread';

let host: HTMLElement;
let component: Record<string, any> | null = null;

function mockApi(threadKind: string) {
	globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/api/delegation/threads/')) {
			return {
				ok: true, status: 200,
				json: async () => ({
					thread_id: CHILD_ID, title: 'tmp inspector', thread_kind: threadKind,
					driver: 'cc', model: 'cc/opus', cc_live: true, codex_live: false,
					busy: false, turn_active: false,
				}),
			};
		}
		return { ok: true, status: 200, json: async () => ({ messages: [], has_more: false, next_cursor: null }) };
	}) as unknown as typeof fetch;
}

beforeEach(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(), cancel: vi.fn(), commitStyles: vi.fn(),
			onfinish: null, playState: 'finished',
		})) as unknown as Element['animate'];
	}
	token.set('tok');
	threads.set([{ id: CHILD_ID, title: 'tmp inspector' } as never]);
	activeThreadId.set(null);
	messages.set([]);
	subagentMeta.set(new Map());
	_resetTurnStateForTests();
	_resetCheckpointsForTests();
	_resetSubagentHydrationForTests();
	clearAllContextUsage();
	reasoningEffort.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
	_resetCheckpointsForTests();
	globalThis.fetch = originalFetch;
	activeThreadId.set(null);
});

function renderChatPanel(): Record<string, any> {
	globalThis.fetch = withEmptyAttentionQueue(globalThis.fetch);
	component = mount(ChatPanel, {
		target: host,
		props: { onOpenLightbox: () => {} },
	});
	flushSync();
	return component!;
}

describe('ChatPanel subagent hydration', () => {
	it('learns from the server that the opened thread is a worker', async () => {
		mockApi('subagent');
		const panel = renderChatPanel();

		await panel.selectThread(CHILD_ID);
		flushSync();

		expect(get(subagentMeta).get(CHILD_ID)).toMatchObject({
			childThreadId: CHILD_ID,
			name: 'tmp inspector',
			provider: 'cc',
			status: 'ready',
		});
	});

	it('leaves an ordinary thread out of the subagent map', async () => {
		mockApi('chat');
		const panel = renderChatPanel();

		await panel.selectThread(CHILD_ID);
		flushSync();

		expect(get(subagentMeta).has(CHILD_ID)).toBe(false);
	});
});
