/**
 * JOURNEY J2 (client) — a turn runs a tool, and then the turn ends.
 *
 * THE DEFECT THIS ENCODES (USER-REPORTS #6a). A thread showed a spinning
 * "Ran a command / Running" block and a Stop button while the turn was
 * durably `interrupted` and zero turns were active anywhere. The tool result
 * was already saved; only the render was stuck. A test proved the block
 * appears. Nothing proved it goes away — and "it goes away" has three
 * distinct causes that must each be sufficient on their own:
 *
 *   1. the turn's terminal frame arrives (ordinary case),
 *   2. the turn is cancelled by Stop (the user is watching for this one),
 *   3. no frame arrives at all and the client reconnects — the case that
 *      actually broke, because it depends on nothing being delivered.
 *
 * P1 (disappearance) and A1 (no spinner outlives its cause), tested at the
 * only level that can see a spinner.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import {
	activeThreadId,
	activeToolsByThread,
	messages,
	type ActiveTool,
} from '$lib/stores/threads';
import { handleMessage } from '$lib/ws/handlers';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';
import { coreSettings } from '$lib/stores/settings';
import MessageList from '$lib/components/MessageList.svelte';

const THREAD_ID = 'journey-tool-thread';
const TURN = { turnId: 'turn-7', turnOrder: 7 };

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(): void {
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined, userIsNearBottom: true, ignoreScrollEvents: false,
			onOpenLightbox: () => {},
		},
	});
	flushSync();
}

/** A tool the client believes is running right now. */
function runningTool(): ActiveTool {
	return {
		id: 'tc_0', name: 'read_file', hint: '', status: 'running',
		args: '{"path":"probe.txt"}',
	};
}

function spinning(): boolean {
	return host.querySelector('.ta-block.running') !== null;
}

function startToolTurn(): void {
	applyLifecycle(THREAD_ID, TURN);
	activeToolsByThread.set(new Map([[THREAD_ID, [runningTool()]]]));
	render();
	expect(spinning()).toBe(true);
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	messages.set([]);
	activeToolsByThread.set(new Map());
	_resetTurnStateForTests();
	coreSettings.set({ features: { focus_mode: false } } as any);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	messages.set([]);
	activeToolsByThread.set(new Map());
	_resetTurnStateForTests();
	coreSettings.set(null);
});

describe('J2 — the tool block stops spinning when its turn stops', () => {
	it('clears when the turn completes normally', () => {
		startToolTurn();

		handleMessage({
			type: 'turn_complete', thread_id: THREAD_ID,
			turn_id: TURN.turnId, turn_order: TURN.turnOrder,
		}, () => {});
		flushSync();

		expect(spinning()).toBe(false);
	});

	it('clears when the user presses Stop and the turn is cancelled', () => {
		startToolTurn();

		handleMessage({
			type: 'turn_cancelled', thread_id: THREAD_ID,
			turn_id: TURN.turnId, turn_order: TURN.turnOrder,
		}, () => {});
		flushSync();

		expect(spinning()).toBe(false);
	});

	it('clears when the turn was interrupted mid-tool', () => {
		// The exact shape of the report: the turn ended while a tool was still
		// the last thing the client heard about.
		startToolTurn();

		handleMessage({
			type: 'turn_interrupted', thread_id: THREAD_ID,
			turn_id: TURN.turnId, turn_order: TURN.turnOrder,
		}, () => {});
		flushSync();

		expect(spinning()).toBe(false);
	});

	it('clears on reconnect when NO terminal frame ever arrived (the real one)', () => {
		// This is the case the product owner hit. The turn ended, the completion
		// event never reached this client, and the block span forever because
		// clearing depended on a frame that was never delivered. Reopening the
		// thread hands the client a snapshot saying "nothing is running"; that
		// alone must be enough.
		startToolTurn();

		handleMessage({
			type: 'session_snapshot', thread_id: THREAD_ID,
			turn_id: null, turn_order: null, state: null,
		}, () => {});
		flushSync();

		expect(spinning()).toBe(false);
	});

	it('keeps spinning while the turn really is running (the counter-case)', () => {
		// Without this, "never show a spinner" would pass every test above.
		startToolTurn();

		handleMessage({
			type: 'turn_activity', thread_id: THREAD_ID,
			turn_id: TURN.turnId, turn_order: TURN.turnOrder,
		}, () => {});
		flushSync();

		expect(spinning()).toBe(true);
	});

	it('ignores a terminal frame from a turn that is not the live one', () => {
		// P9 — nothing else moved. A stale terminal for an older turn must not
		// blank the block of the turn that is genuinely running now.
		startToolTurn();

		handleMessage({
			type: 'turn_complete', thread_id: THREAD_ID,
			turn_id: 'turn-6', turn_order: 6,
		}, () => {});
		flushSync();

		expect(spinning()).toBe(true);
	});
});
