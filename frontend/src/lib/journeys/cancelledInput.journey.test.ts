/**
 * JOURNEY J5 (client) — take a message back out of the queue, then read it.
 *
 * THE DEFECT THIS ENCODED (fixed). Cancelling was verified to change a state.
 * Nobody read the bubble afterwards, and the bubble was wrong: a cancelled
 * message showed NO TIMESTAMP, permanently, because the time was printed only
 * when `deliveryState === null` and a cancelled row is `cancelled` forever.
 * It was the last message the user sent and the thread could not say when.
 *
 * A cancelled message is TERMINAL, not in progress. Suppressing the time while
 * a message is still moving is a decision; suppressing it after it has stopped
 * moving was the bug. The gate is now `deliveryStillMoving`.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { token } from '$lib/stores/auth';
import { activeThreadId } from '$lib/stores/threads';
import { isMachineCode } from '$lib/plainError';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import UserMessageBubble from '$lib/components/UserMessageBubble.svelte';

const THREAD_ID = 'journey-thread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function bubble(overrides: Record<string, unknown> = {}): void {
	component = mount(UserMessageBubble, {
		target: host,
		props: {
			msg: {
				id: 'msg-1', thread_id: THREAD_ID, role: 'user',
				content: 'taken back out of the line',
				created_at: '2026-08-10T05:15:44.969405',
				metadata: { input_id: 'i1', delivery_intent: 'queue_after_turn' },
				delivery_state: 'cancelled', delivery_error: 'cancelled_by_user',
				delivery_revision: 2, seq: 1,
				...overrides,
			},
			onOpenLightbox: () => {},
		},
	});
	flushSync();
}

/** Everything in the bubble's status line, in reading order. */
function statusLine(): string {
	return host.querySelector<HTMLElement>('.msg-time')!.textContent!.replace(/\s+/g, ' ').trim();
}

function controls(): string[] {
	return [...host.querySelectorAll<HTMLElement>('[data-delivery-action]')]
		.map((el) => el.dataset.deliveryAction ?? '');
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('token');
	activeThreadId.set(THREAD_ID);
	_resetTurnStateForTests();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	activeThreadId.set(null);
	_resetTurnStateForTests();
});

describe('J5 — a message the user cancelled', () => {
	it('says what happened to it, in words, with no machine code', () => {
		bubble();

		expect(statusLine()).toContain('Cancelled by you');
		expect(statusLine()).not.toContain('cancelled_by_user');
		for (const word of statusLine().split(' ')) {
			expect(isMachineCode(word)).toBe(false);
		}
	});

	it('offers no control that would do nothing (A7)', () => {
		// Retry belongs to states that could still move. A cancellation is the
		// user's own final answer, so a Retry here would be a button that either
		// does nothing or undoes a decision they made on purpose.
		bubble();

		expect(controls()).toEqual([]);
	});

	it('shows the time it was written, like every other message', () => {
		// Was `it.fails`. UserMessageBubble.svelte printed the time only when
		// `deliveryState === null`, and a cancelled row is `cancelled`, so the
		// branch never ran. The gate is now "is this still moving?", which is
		// the question that was meant all along: `cancelled` is an ENDING, and
		// an ending has a time. The row always held it — the backend journey
		// asserts the server keeps `created_at` unchanged through the cancel.
		bubble();

		expect(statusLine()).toMatch(/\d{1,2}:\d{2}/);
	});

	it('keeps the time on a message that is still moving out of the way', () => {
		// The counter-case that makes the fix safe rather than a blanket change:
		// while a message is genuinely in progress the descriptor is the news,
		// and this test pins that the two cases are allowed to differ.
		bubble({ delivery_state: 'queued', delivery_error: null, seq: null });

		expect(statusLine()).toContain('Queued');
	});
});

describe('J5 — every ending a message can reach says something a human can act on', () => {
	// A3 / P10. Codes are the backend's vocabulary; this is the one gate before
	// the user's eyes. The last case is the point: a code this client has never
	// seen must still produce a sentence, because the alternative is that the
	// next backend error ships straight to the screen.
	// Third column is the exit the ending is entitled to (A7): the user's own
	// cancellation needs none, an unconfirmed write needs the resend that warns
	// it may duplicate, and everything else needs a plain Retry.
	const endings: Array<[string, string, string, string[]]> = [
		['cancelled', 'cancelled_by_user', 'Cancelled by you', []],
		['ambiguous', 'provider_write_outcome_unknown', 'Delivery uncertain', ['retry-anyway']],
		['ambiguous', 'provider_write_abandoned', 'Delivery uncertain', ['retry-anyway']],
		['failed', 'delivery_descriptor_missing', 'Not delivered', ['retry']],
		['failed', 'missing_context_evidence', 'Not delivered', ['retry']],
		['failed', 'target_invalid', 'Not delivered', ['retry']],
		['failed', 'provider_unavailable', 'Not delivered', ['retry']],
		['failed', 'a_code_this_client_has_never_seen', 'Not delivered', ['retry']],
	];

	for (const [deliveryState, code, expected, exits] of endings) {
		it(`renders ${code} as words`, () => {
			bubble({ delivery_state: deliveryState, delivery_error: code });

			const line = statusLine();
			expect(line).toContain(expected);
			expect(line).not.toContain(code);
			expect(line.split(' ').some((word) => isMachineCode(word))).toBe(false);
			expect(controls()).toEqual(exits);
		});
	}

	it('warns that a resend may duplicate when the provider may already hold it', () => {
		bubble({ delivery_state: 'ambiguous', delivery_error: 'provider_write_ambiguous' });

		expect(statusLine()).toContain('Delivery uncertain');
		expect(controls()).toEqual(['retry-anyway']);
		expect(
			host.querySelector<HTMLElement>('[data-delivery-action]')!.title,
		).toContain('twice');
	});
});
