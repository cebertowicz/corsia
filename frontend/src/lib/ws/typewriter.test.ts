import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '$lib/stores/threads';
import { applyLifecycle, _resetTurnStateForTests } from '$lib/ws/turnState';
import { startTypewriter } from '$lib/ws/typewriter';

const THREAD_ID = 'typewriter-session';
const IDENTITY = { turnId: 'turn-1', turnOrder: 1 };

function seedMessage(pos: number | undefined, content: string) {
	messages.set([{
		id: 'assistant-1',
		thread_id: THREAD_ID,
		role: 'assistant',
		content,
		created_at: '2026-08-10T05:00:00Z',
		typewriterPos: pos,
	} as any]);
}

function revealedPos(): number | undefined {
	return get(messages).find((m: any) => m.id === 'assistant-1')?.typewriterPos;
}

beforeEach(() => {
	vi.useFakeTimers();
	_resetTurnStateForTests();
	activeThreadId.set(THREAD_ID);
	applyLifecycle(THREAD_ID, IDENTITY);
});

afterEach(() => {
	vi.useRealTimers();
	messages.set([]);
	activeThreadId.set(null);
	_resetTurnStateForTests();
});

describe('typewriter reveal', () => {
	it('never leaves a message part-revealed when its turn ends', () => {
		// The reported defect: an assistant message that was not streamed live
		// starts at position 0, so it renders as NOTHING until the animation
		// advances. When the turn ended the animation used to just stop —
		// leaving the message intact in the store and invisible on screen until
		// some later render happened to move it. Decoration must never be the
		// reason content is missing.
		const content = 'a message the user was trying to read';
		seedMessage(0, content);
		startTypewriter(THREAD_ID, 'assistant-1', 0, content.length, IDENTITY);

		vi.advanceTimersByTime(40);
		expect(revealedPos()).toBeGreaterThan(0);
		expect(revealedPos()).toBeLessThan(content.length);

		// The turn ends mid-reveal.
		applyLifecycle(THREAD_ID, { turnId: 'turn-2', turnOrder: 2 });
		vi.advanceTimersByTime(40);

		expect(revealedPos()).toBeUndefined();
	});

	it('never leaves a message part-revealed when the user switches threads', () => {
		const content = 'another message mid-reveal';
		seedMessage(0, content);
		startTypewriter(THREAD_ID, 'assistant-1', 0, content.length, IDENTITY);
		vi.advanceTimersByTime(40);

		activeThreadId.set('some-other-thread');
		vi.advanceTimersByTime(40);

		expect(revealedPos()).toBeUndefined();
	});

	it('still reveals normally to the end when nothing interrupts it', () => {
		const content = 'an uninterrupted reveal';
		seedMessage(0, content);
		startTypewriter(THREAD_ID, 'assistant-1', 0, content.length, IDENTITY);

		vi.advanceTimersByTime(5_000);

		expect(revealedPos()).toBeUndefined();
	});
});

describe('content is never hidden by something that will not reveal it', () => {
	/**
	 * The reported "my last output disappears and comes back when the next
	 * message starts streaming", and the half the first fix missed.
	 *
	 * `handleStepText` set `typewriterPos: 0` on the row whenever the content
	 * had not streamed live, but started a reveal only when the row was NEWLY
	 * inserted. A row that already existed — history load, an earlier
	 * `message_added`, a re-delivered snapshot — was therefore hidden with
	 * nothing running to un-hide it. No timer meant the abandon path could
	 * never fire either, so it stayed invisible until an unrelated update
	 * happened to rewrite it.
	 */
	it('leaves an already-present message visible when no reveal starts', async () => {
		const { handleStepText } = await import('$lib/ws/handlersTurns');
		// The outer beforeEach already applied this turn's lifecycle, and a
		// second apply of the same identity is 'ignored' — which makes the
		// handler bail before it reaches the code under test. Start clean so the
		// handler's own admission is the first one.
		_resetTurnStateForTests();
		const content = 'the answer the user must keep seeing';
		// The row is already in the store, exactly as a history load leaves it —
		// with OLDER content, so the assertions below can prove the handler
		// actually ran rather than passing because it bailed out early.
		messages.set([{
			id: 'assistant-1', thread_id: THREAD_ID, role: 'assistant',
			content: 'stale placeholder', created_at: '2026-08-10T00:00:00Z',
		}] as never);

		handleStepText({
			thread_id: THREAD_ID,
			turn_id: IDENTITY.turnId,
			turn_order: IDENTITY.turnOrder,
			message_id: 'assistant-1',
			created_at: '2026-08-10T00:00:00Z',
			content,
			metadata: {},
		});

		const row = get(messages).find(m => m.id === 'assistant-1');
		// Proof the handler actually ran, so the assertion below means something.
		expect(row?.content).toBe(content);
		expect(row?.typewriterPos).toBeUndefined();
	});
});
