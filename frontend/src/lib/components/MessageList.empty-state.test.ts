import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { _resetTurnStateForTests, applyLifecycle, applyTerminal } from '$lib/ws/turnState';
import { coreSettings, type CoreSettings } from '$lib/stores/settings';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
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
	coreSettings.set(null);
	threadPlanStates.set({});
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

describe('an empty thread whose plan waits outside', () => {
	it('shows the waiting chip where the first run would sit, not the greeting (EW8)', () => {
		coreSettings.set({ features: { focus_mode: true } } as unknown as CoreSettings);
		threadPlanStates.set({
			[THREAD_ID]: {
				state: 'waiting', label_key: null, paused: false, attention_items: [],
				wait: { label: 'CI on PR #1193', until: new Date(Date.now() + 40 * 60_000).toISOString(), more: 0 },
			},
		});
		renderList();

		expect(greeting()).toBeNull();
		expect(host.querySelector('.fold-toggle')?.textContent).toContain('Waiting');
		expect(host.textContent).toContain('CI on PR #1193');
	});
});

describe('a sent input the server holds hides the wait face (EW8)', () => {
	const reply = {
		id: 'm1', thread_id: THREAD_ID, role: 'assistant', content: 'I opened the PR.',
		created_at: '2026-10-21T18:00:00Z', seq: 1, turn_id: 'turn-1',
	};
	const queued = {
		id: 'm2', thread_id: THREAD_ID, role: 'user', content: 'Also check the docs.',
		created_at: '2026-10-21T18:01:00Z', delivery_state: 'queued',
	};

	function waitingAfterTerminalTurn(rows: unknown[]): void {
		coreSettings.set({ features: { focus_mode: true } } as unknown as CoreSettings);
		applyLifecycle(THREAD_ID, { turnId: 'turn-1', turnOrder: 1 });
		applyTerminal(THREAD_ID, { turnId: 'turn-1', turnOrder: 1 });
		threadPlanStates.set({
			[THREAD_ID]: {
				state: 'waiting', label_key: null, paused: false, attention_items: [],
				wait: { label: 'CI on PR #1193', until: new Date(Date.now() + 40 * 60_000).toISOString(), more: 0 },
			},
		});
		messages.set(rows as never);
		renderList();
	}

	it('shows the chip after a terminal turn with no input waiting', () => {
		waitingAfterTerminalTurn([reply]);
		expect(host.textContent).toContain('CI on PR #1193');
	});

	it('shows no chip while a queued input waits, though this tab holds no pending id', () => {
		waitingAfterTerminalTurn([reply, queued]);
		expect(host.textContent).not.toContain('CI on PR #1193');
	});
});
