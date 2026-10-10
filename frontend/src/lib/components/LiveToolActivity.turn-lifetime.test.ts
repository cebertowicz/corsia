import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import LiveToolActivity from './LiveToolActivity.svelte';
import {
	activeThreadId,
	activeToolsByThread,
	messages,
} from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import {
	_resetTurnStateForTests,
	applyLifecycle,
	getTurnState,
} from '$lib/ws/turnState';

const THREAD_ID = 'thread-interrupted';
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

function runningBlock(): HTMLElement | null {
	return host.querySelector<HTMLElement>('.tool-activity');
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	activeToolsByThread.set(new Map());
	messages.set([]);
	_resetTurnStateForTests();
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

describe('a spinner must not outlive its turn', () => {
	it('clears a running tool block when the turn is interrupted mid-tool', () => {
		// USER-REPORTS #6a: the turn ended without the tool's completion event
		// reaching the client, and the block spun forever.
		applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', name: 'mcp__corsia__bash', hint: 'ran a command',
		}, () => {});
		render();
		expect(runningBlock()).not.toBeNull();

		handleMessage({ type: 'turn_interrupted', thread_id: THREAD_ID, ...TURN }, () => {});
		flushSync();

		expect(runningBlock()).toBeNull();
		expect(get(activeToolsByThread).get(THREAD_ID)).toBeUndefined();
	});

	it('clears the block when the Stop receipt says the turn was already over', () => {
		// USER-REPORTS #6: the turn was durably interrupted, the Stop receipt
		// said so, and the UI kept spinning and kept offering Stop — so the user
		// could not tell a working Stop from a broken one. The receipt is the
		// server stating this thread holds no live turn, and it is now believed.
		applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', name: 'mcp__corsia__bash', hint: 'ran a command',
		}, () => {});
		render();
		expect(runningBlock()).not.toBeNull();

		handleMessage({
			type: 'stop_receipt', thread_id: THREAD_ID,
			outcome: 'already_terminal', turn_id: TURN.turn_id,
		}, () => {});
		flushSync();

		expect(getTurnState(THREAD_ID).phase).toBe('terminal');
		expect(runningBlock()).toBeNull();
		expect(get(activeToolsByThread).get(THREAD_ID)).toBeUndefined();
	});

	it('settles the stopped turn even when no terminal frame follows', () => {
		applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', name: 'mcp__corsia__bash', hint: 'ran a command',
		}, () => {});
		render();
		expect(runningBlock()).not.toBeNull();

		handleMessage({
			type: 'stop_receipt', thread_id: THREAD_ID,
			outcome: 'stopped', turn_id: TURN.turn_id,
		}, () => {});
		flushSync();

		expect(getTurnState(THREAD_ID).phase).toBe('terminal');
		expect(runningBlock()).toBeNull();
		expect(get(activeToolsByThread).get(THREAD_ID)).toBeUndefined();
	});

	it('leaves a successor turn alone when a stale Stop receipt arrives', () => {
		// `already_terminal` names the turn it is about. A turn admitted between
		// the Stop and its receipt is a live turn the receipt says nothing about,
		// and settling it would replace one lie with another.
		applyLifecycle(THREAD_ID, { turnId: 'turn-2', turnOrder: 2 });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, turn_id: 'turn-2', turn_order: 2,
			tool_id: 'tool-2', name: 'mcp__corsia__bash', hint: 'ran a command',
		}, () => {});
		render();

		handleMessage({
			type: 'stop_receipt', thread_id: THREAD_ID,
			outcome: 'already_terminal', turn_id: TURN.turn_id,
		}, () => {});
		flushSync();

		expect(getTurnState(THREAD_ID).phase).toBe('active');
		expect(runningBlock()).not.toBeNull();
	});

	it('leaves the current turn active when stopped names its predecessor', () => {
		applyLifecycle(THREAD_ID, { turnId: 'turn-2', turnOrder: 2 });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, turn_id: 'turn-2', turn_order: 2,
			tool_id: 'tool-2', name: 'mcp__corsia__bash', hint: 'ran a command',
		}, () => {});
		render();

		handleMessage({
			type: 'stop_receipt', thread_id: THREAD_ID,
			outcome: 'stopped', turn_id: TURN.turn_id,
		}, () => {});
		flushSync();

		expect(getTurnState(THREAD_ID)).toMatchObject({
			phase: 'active', turnId: 'turn-2', turnOrder: 2,
		});
		expect(runningBlock()).not.toBeNull();
		expect(get(activeToolsByThread).get(THREAD_ID)?.[0]?.id).toBe('tool-2');
	});

	it('renders nothing live for a thread whose turn is already over', () => {
		// Belt for the same brace: the live region belongs to a live turn, so a
		// buffer left behind by any missed terminal cannot paint a spinner.
		activeToolsByThread.set(new Map([[THREAD_ID, [{
			id: 'orphan', name: 'mcp__corsia__bash', hint: 'ran a command',
			status: 'running' as const,
		}]]]));
		render();

		expect(getTurnState(THREAD_ID).phase).toBe('idle');
		expect(runningBlock()).toBeNull();
	});
});
