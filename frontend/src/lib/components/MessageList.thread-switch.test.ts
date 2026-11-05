import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

/**
 * Selection, loading, and the rendered transcript must form one coherent
 * state (issue #1084). The `messages` store is a single global array shared
 * across every thread and is only reconciled to the newly selected thread
 * once its first page resolves. Until that happens, the canvas must never
 * present the prior thread's rows as belonging to the newly selected one —
 * regardless of why they are still sitting in the store.
 */

const PRIOR_THREAD_ID = 'prior-thread';
const NEXT_THREAD_ID = 'next-thread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function priorThreadRow(): Element | null {
	return host.querySelector('[data-message-id="prior-1"]');
}

function renderList(props: Record<string, unknown> = {}): void {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
			...props,
		},
	});
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	messages.set([
		{
			id: 'prior-1',
			thread_id: PRIOR_THREAD_ID,
			role: 'assistant',
			content: 'prior thread answer',
			created_at: '2026-08-01T00:00:00Z',
		},
	] as never);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	messages.set([]);
	_resetTurnStateForTests();
});

describe('switching threads never presents the prior thread as the newly selected one', () => {
	it('hides the prior thread and shows the loading treatment while the new thread loads', () => {
		activeThreadId.set(NEXT_THREAD_ID);
		renderList({ firstLoadPending: true });

		expect(priorThreadRow()).toBeNull();
		expect(host.textContent).not.toContain('prior thread answer');
		expect(host.textContent).toContain('Loading this chat');
	});

	it('never renders another thread\'s rows under the active selection, independent of load-state flags', () => {
		// Defense in depth: even without firstLoadPending/firstLoadFailed set,
		// the canvas must not render a row whose thread_id does not match the
		// current selection — the render surface owns this invariant, not just
		// the loading-flag gates.
		activeThreadId.set(NEXT_THREAD_ID);
		renderList();

		expect(priorThreadRow()).toBeNull();
		expect(host.textContent).not.toContain('prior thread answer');
	});

	it('still renders the active thread\'s own messages once loaded', () => {
		messages.set([
			{
				id: 'next-1',
				thread_id: NEXT_THREAD_ID,
				role: 'assistant',
				content: 'next thread answer',
				created_at: '2026-08-02T00:00:00Z',
			},
		] as never);
		activeThreadId.set(NEXT_THREAD_ID);
		renderList();

		expect(host.querySelector('[data-message-id="next-1"]')).not.toBeNull();
		expect(host.textContent).toContain('next thread answer');
	});
});
