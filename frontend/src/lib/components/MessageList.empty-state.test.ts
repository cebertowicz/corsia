import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

/**
 * An empty canvas is a claim about the thread. It may only be made when the
 * thread is genuinely empty — never while the first page is still in flight,
 * and never after that page failed, when the transcript is unknown rather than
 * absent and the user needs a way back.
 */

const THREAD_ID = 'empty-state-session';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function greeting(): Element | null {
	return host.querySelector('.empty-chat');
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
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	messages.set([]);
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

describe('the empty canvas is a three-way, not a default', () => {
	it('greets only a thread that is genuinely empty', () => {
		renderList();
		expect(greeting()).not.toBeNull();
	});

	it('does not greet while the first page is still loading', () => {
		renderList({ firstLoadPending: true });
		expect(greeting()).toBeNull();
		expect(host.textContent).toContain('Loading this thread');
	});

	it('does not greet after the first page failed, and offers a retry', () => {
		const onRetryFirstLoad = vi.fn();
		renderList({ firstLoadFailed: true, onRetryFirstLoad });

		expect(greeting()).toBeNull();
		expect(host.textContent).toContain("Couldn't load this thread");

		const retry = host.querySelector<HTMLButtonElement>('[aria-label="Retry loading this thread"]');
		expect(retry).not.toBeNull();
		retry!.click();
		flushSync();
		expect(onRetryFirstLoad).toHaveBeenCalledOnce();
	});

	it('a failure outranks a load still in flight — the last word wins', () => {
		renderList({ firstLoadPending: true, firstLoadFailed: true });
		expect(host.textContent).toContain("Couldn't load this thread");
		expect(host.textContent).not.toContain('Loading this thread');
	});

	it('keeps a failed refresh visible above a transcript that already has rows', () => {
		messages.set([
			{
				id: 'm1', thread_id: THREAD_ID, role: 'assistant',
				content: 'earlier answer', created_at: '2026-08-01T00:00:00Z',
			},
		] as never);
		const onRetryFirstLoad = vi.fn();
		renderList({ firstLoadFailed: true, onRetryFirstLoad });

		expect(greeting()).toBeNull();
		expect(host.querySelector('[data-message-id="m1"]')).not.toBeNull();
		const retry = host.querySelector<HTMLButtonElement>('[aria-label="Retry loading this thread"]');
		expect(retry).not.toBeNull();
		retry!.click();
		flushSync();
		expect(onRetryFirstLoad).toHaveBeenCalledOnce();
	});
});
