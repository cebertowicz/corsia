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

it('shows the introduction and never the tagged answer or a partial tag while it streams', () => {
	applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
	const delta = (content: string, seq: number) => handleMessage({ type: 'text_delta', thread_id: THREAD_ID, ...TURN, content, seq }, () => {});
	delta('Here is the plan.\n\n[c18', 1);
	expect(shown()).toContain('Here is the plan.');
	expect(shown()).not.toContain('[c18');
	delta('.1] The answer lives in its thread.', 2);
	expect(shown()).toContain('Here is the plan.');
	expect(shown()).not.toContain('answer lives');
	expect(shown()).not.toContain('[c18.1]');
});

it('keeps an answer-only stream to the working indicator', () => {
	applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
	handleMessage({ type: 'text_delta', thread_id: THREAD_ID, ...TURN, content: '[c3.1] Only an answer.', seq: 1 }, () => {});
	expect(shown()).not.toContain('Only an answer');
	expect(host.querySelector('.thinking-indicator')).not.toBeNull();
});

it('hides the answer replayed from the turn buffer after a reconnect', () => {
	beginPendingSnapshot(THREAD_ID);
	handleMessage({
		type: 'session_snapshot', thread_id: THREAD_ID, ...TURN,
		state: { content: 'Here is the plan.\n\n[c18.1] The answer lives', reasoning: '', active_tools: [], seq: 1 },
	}, () => {});
	handleMessage({ type: 'text_delta', thread_id: THREAD_ID, ...TURN, content: ' in its thread.', seq: 2 }, () => {});
	expect(shown()).toContain('Here is the plan.');
	expect(shown()).not.toContain('answer lives');
});
