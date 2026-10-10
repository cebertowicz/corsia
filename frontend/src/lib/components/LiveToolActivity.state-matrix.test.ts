/**
 * #1083 — remaining state-matrix coverage for the quiet-gap indicator fix.
 * `LiveToolActivity.post-tool-gap.test.ts` is the accepted fail-first proof
 * for the core defect and stays untouched; this covers the neighboring
 * states so the fix doesn't trade one falsehood for another.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import LiveToolActivity from './LiveToolActivity.svelte';
import {
	activeThreadId,
	activeToolsByThread,
	liveReasoningByThread,
	messages,
	streamContentByThread,
} from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';

const THREAD_ID = 'thread-state-matrix';
const TURN = { turn_id: 'turn-1', turn_order: 1 };

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(): void {
	component = mount(LiveToolActivity, {
		target: host,
		props: { expandedTools: new Set<string>(), onToggleToolBlock: () => {} },
	});
	flushSync();
}

function dots(): HTMLElement | null {
	return host.querySelector<HTMLElement>('.thinking-indicator');
}

function spinner(): HTMLElement | null {
	return host.querySelector<HTMLElement>('.ta-spin');
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	activeToolsByThread.set(new Map());
	liveReasoningByThread.set(new Map());
	streamContentByThread.set(new Map());
	messages.set([]);
	_resetTurnStateForTests();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	activeToolsByThread.set(new Map());
	liveReasoningByThread.set(new Map());
	streamContentByThread.set(new Map());
	messages.set([]);
	_resetTurnStateForTests();
});

describe('#1083 — quiet-gap indicator does not fire when something else already proves liveness', () => {
	it('stays silent while one of several tools is still running', () => {
		applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', name: 'read_file', hint: 'read a file',
		}, () => {});
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-2', name: 'grep', hint: 'search',
		}, () => {});
		handleMessage({
			type: 'tool_done', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', result: 'ok',
		}, () => {});
		render();

		// tool-2 is still running: the real spinner already tells the truth,
		// a second redundant affordance would be noise, not more truth.
		expect(spinner()).not.toBeNull();
		expect(dots()).toBeNull();
	});

	it('stays silent once reasoning starts streaming for the next step', () => {
		applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', name: 'read_file', hint: 'read a file',
		}, () => {});
		handleMessage({
			type: 'tool_done', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', result: 'ok',
		}, () => {});
		render();
		expect(dots()).not.toBeNull();

		handleMessage({
			type: 'step_reasoning', thread_id: THREAD_ID, ...TURN,
			content: 'considering the next step',
		}, () => {});
		flushSync();

		// The reasoning box IS the live affordance now; the dots would just
		// duplicate it.
		expect(host.querySelector('.live-reasoning')).not.toBeNull();
		expect(dots()).toBeNull();
	});

	it('resumes the running spinner when a new tool call starts after the quiet gap', () => {
		applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', name: 'read_file', hint: 'read a file',
		}, () => {});
		handleMessage({
			type: 'tool_done', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', result: 'ok',
		}, () => {});
		render();
		expect(dots()).not.toBeNull();

		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-2', name: 'grep', hint: 'search',
		}, () => {});
		flushSync();

		expect(spinner()).not.toBeNull();
		expect(dots()).toBeNull();
	});
});
