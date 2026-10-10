import { flushSync, mount, tick, unmount } from 'svelte';
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
 * New chat clears the active thread and its rows. The older-history pager of
 * the thread it showed must not outlive it: a stale `hasMore` makes the next
 * scroll fetch an older page for no thread and print a load error on an empty
 * canvas.
 */

const originalFetch = globalThis.fetch;

let host: HTMLElement;
let component: Record<string, any> | null = null;

function firstPageWithMoreHistory() {
	globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/messages?')) {
			return {
				ok: true,
				status: 200,
				json: async () => ({
					messages: [{
						id: 'b1', thread_id: 'thread-b', role: 'assistant',
						content: 'newest', created_at: '2026-08-01T00:00:00Z',
					}],
					has_more: true,
					oldest_id: 'b1',
				}),
			};
		}
		return { ok: true, status: 200, json: async () => ({ thread_kind: 'chat' }) };
	}) as unknown as typeof fetch;
	globalThis.fetch = withEmptyAttentionQueue(globalThis.fetch);
}

beforeEach(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(), cancel: vi.fn(), commitStyles: vi.fn(),
			onfinish: null, playState: 'finished',
		})) as unknown as Element['animate'];
	}
	vi.spyOn(console, 'error').mockImplementation(() => {});
	token.set('tok');
	threads.set([{ id: 'thread-b', title: 'Thread B' } as never]);
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
	messages.set([]);
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
	_resetCheckpointsForTests();
	globalThis.fetch = originalFetch;
	activeThreadId.set(null);
});

describe('ChatPanel new chat', () => {
	it('does not commit a first-run opener after the websocket import yields to thread-to-null intent', async () => {
		firstPageWithMoreHistory();
		threads.set([
			{ id: 'thread-a', title: 'Thread A' } as never,
			{ id: 'thread-b', title: 'Thread B' } as never,
		]);
		activeThreadId.set('thread-a');
		component = mount(ChatPanel, {
			target: host,
			props: { onOpenLightbox: () => {} },
		});
		flushSync();
		let ownsFirstRun = true;
		const opening = (component as Record<string, any>).selectThread(
			'thread-b', 'other', () => ownsFirstRun,
		);
		activeThreadId.set(null);
		ownsFirstRun = false;
		await opening;
		expect(get(activeThreadId)).toBeNull();
	});

	it('does not report a failed older-history load on the empty canvas', async () => {
		firstPageWithMoreHistory();
		component = mount(ChatPanel, {
			target: host,
			props: { onOpenLightbox: () => {} },
		});
		flushSync();
		await (component as Record<string, any>).selectThread('thread-b');
		flushSync();

		// What "New chat" does: no active thread, no rows.
		activeThreadId.set(null);
		messages.set([]);
		flushSync();
		await tick();

		// Let the programmatic scroll-to-bottom release its scroll-event guard.
		await new Promise((resolve) => setTimeout(resolve, 50));

		host.querySelector('.messages')?.dispatchEvent(new Event('scroll'));
		flushSync();
		await tick();

		await new Promise((resolve) => setTimeout(resolve, 50));
		flushSync();

		expect(host.textContent).not.toContain("Couldn't load older messages");
	});
});
