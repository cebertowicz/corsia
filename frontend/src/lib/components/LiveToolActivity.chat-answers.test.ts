/** Answers to chat comments never stream into chat, live or replayed after a reconnect. */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it } from 'vitest';

import LiveToolActivity from './LiveToolActivity.svelte';
import { activeThreadId, activeToolsByThread, liveReasoningByThread, messages, streamContentByThread } from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import { beginPendingSnapshot } from '$lib/ws/streamState';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';

const THREAD_ID = 'thread-chat-answers';
const TURN = { turn_id: 'turn-1', turn_order: 1 };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function reset() {
	activeToolsByThread.set(new Map());
	liveReasoningByThread.set(new Map());
	streamContentByThread.set(new Map());
	messages.set([]);
	_resetTurnStateForTests();
}

function shown(): string {
	flushSync();
	return host.textContent ?? '';
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	reset();
	component = mount(LiveToolActivity, { target: host, props: { expandedTools: new Set<string>(), onToggleToolBlock: () => {} } });
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	reset();
});

it('renders the visible text the server streams', () => {
	applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
	handleMessage({ type: 'text_delta', thread_id: THREAD_ID, ...TURN, content: 'Here is the plan.', seq: 1 }, () => {});
	expect(shown()).toContain('Here is the plan.');
});

it('keeps an empty stream to the working indicator', () => {
	applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
	shown();
	expect(host.querySelector('.thinking-indicator')).not.toBeNull();
});

it('renders the visible prefix supplied in a reconnect snapshot', () => {
	beginPendingSnapshot(THREAD_ID);
	handleMessage({
		type: 'session_snapshot', thread_id: THREAD_ID, ...TURN,
		state: { content: 'Here is the plan.', reasoning: '', active_tools: [], seq: 1 },
	}, () => {});
	expect(shown()).toContain('Here is the plan.');
});
