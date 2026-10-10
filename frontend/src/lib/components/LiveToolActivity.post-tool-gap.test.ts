/**
 * #1083 — a live tool-call turn goes quiet between tool completion and the
 * model's next output.
 *
 * `LiveToolActivity` picks ONE mutually exclusive branch: the tool-block
 * branch (`showLiveTools = $isStreaming && $activeTools.length > 0`) renders
 * whenever any tool is known to this turn, even after every one of them has
 * reached `status: 'done'`. Once that branch wins, the thinking-indicator
 * dots in the sibling `:else if $isStreaming` branch can never render — they
 * are unreachable while a finished tool is still sitting in
 * `activeToolsByThread`.
 *
 * On the real backend (`core/turns_steps.py::_execute_tool_calls_inner`),
 * finishing a tool immediately loops back into another `call_llm` for the
 * turn's next step. Nothing clears the finished tool from
 * `activeToolsByThread` until the NEXT `step_text` frame's
 * `finalized_tool_ids` arrives, or a terminal frame clears the buffer
 * outright. Between "tool finished" and "next content/reasoning chunk
 * arrives", the turn is genuinely active (`isStreaming` stays true — the
 * canonical owner in `turnState.ts` is untouched) but the UI renders a
 * static, non-animating checkmark row with no live affordance anywhere on
 * screen. That silent gap is exactly "the typing indicator appears to
 * disappear... the thread can look idle even though the turn is still
 * active" from the report.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import LiveToolActivity from './LiveToolActivity.svelte';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';

const THREAD_ID = 'thread-post-tool-gap';
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

/** Any element telling the user the turn is still alive right now. */
function liveAffordance(): HTMLElement | null {
	return host.querySelector<HTMLElement>('.thinking-indicator, .ta-spin');
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

describe('#1083 — the live indicator must not go quiet mid-turn', () => {
	it('shows a live affordance after a tool completes but the turn is still generating', () => {
		applyLifecycle(THREAD_ID, { turnId: TURN.turn_id, turnOrder: TURN.turn_order });
		handleMessage({
			type: 'tool_start', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', name: 'read_file', hint: 'read a file',
		}, () => {});
		render();
		expect(host.querySelector('.ta-spin')).not.toBeNull();

		// The tool finishes. The turn is still active (no terminal frame, no
		// text/reasoning chunk has arrived yet) — the real gap between
		// `tool_complete` and the model's next output on a local provider.
		handleMessage({
			type: 'tool_done', thread_id: THREAD_ID, ...TURN,
			tool_id: 'tool-1', result: 'file contents',
		}, () => {});
		flushSync();

		expect(liveAffordance()).not.toBeNull();
	});
});
