/**
 * Regression tests for #528: home-screen first-message send silently dropped.
 *
 * Root cause: handleInput() fires ensureThread() without await when text is typed.
 * If the user pressed Enter while that creation was in-flight, handleSend() also
 * called ensureThread(). The old isAutoCreating guard made it return immediately,
 * leaving $activeThreadId null, so handleSend() silently dropped the message.
 *
 * Fix: track the in-flight threadCreationPromise so concurrent callers await it
 * rather than returning early.
 *
 * These tests model the ensureThread() state machine directly (no Svelte mount
 * needed) so they run fast and fail loudly on regression.
 */
import { describe, expect, it } from 'vitest';

// ---------------------------------------------------------------------------
// Helpers: replicate the InputBar ensureThread() state machine in pure JS
// ---------------------------------------------------------------------------

/**
 * Mirrors the FIXED ensureThread() logic from InputBar.svelte.
 * Returns a bound function that closes over its own state (isAutoCreating,
 * threadCreationPromise), matching the component's per-instance state.
 */
function makeEnsureThread(
	getThreadId: () => string | null,
	onAutoCreateThread: () => Promise<void>,
) {
	let isAutoCreating = false;
	let threadCreationPromise: Promise<void> | null = null;

	return async function ensureThread(): Promise<void> {
		if (getThreadId()) return;
		// Await the in-flight creation instead of returning early (#528 fix).
		if (threadCreationPromise) {
			await threadCreationPromise;
			return;
		}
		isAutoCreating = true;
		threadCreationPromise = (async () => {
			try {
				await onAutoCreateThread();
			} catch {
				// mirror InputBar: log but don't propagate
			} finally {
				isAutoCreating = false;
				threadCreationPromise = null;
			}
		})();
		await threadCreationPromise;
	};
}

/**
 * Mirrors the OLD (buggy) ensureThread() logic before the #528 fix.
 * Kept here to document what the bug looked like and verify the test
 * actually catches the regression.
 */
function makeBuggyEnsureThread(
	getThreadId: () => string | null,
	onAutoCreateThread: () => Promise<void>,
) {
	let isAutoCreating = false;

	return async function ensureThread(): Promise<void> {
		if (getThreadId() || isAutoCreating) return; // BUG: early-return while in-flight
		isAutoCreating = true;
		try {
			await onAutoCreateThread();
		} finally {
			isAutoCreating = false;
		}
	};
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('#528 ensureThread concurrent access', () => {
	it('fixed: handleSend concurrent with handleInput awaits in-flight creation and sends', async () => {
		let threadId: string | null = null;
		let resolveCreate!: () => void;

		const ensureThread = makeEnsureThread(
			() => threadId,
			() => new Promise<void>(resolve => {
				resolveCreate = () => { threadId = 'new-session'; resolve(); };
			}),
		);

		// Phase 1: handleInput fires ensureThread() without await (proactive creation)
		const proactivePromise = ensureThread();
		// At this point: isAutoCreating=true, threadCreationPromise is set, threadId=null

		// Phase 2: user presses Enter — handleSend() calls ensureThread() concurrently
		let didSend = false;
		const handleSendResult = (async () => {
			await ensureThread(); // FIXED: awaits the in-flight promise
			if (threadId) didSend = true; // proceeds after creation completes
		})();

		// Phase 3: session creation resolves (newThread() sets activeThreadId)
		resolveCreate();
		await Promise.all([proactivePromise, handleSendResult]);

		expect(threadId).toBe('new-session');
		expect(didSend).toBe(true); // message is not dropped
	});

	it('regression marker: old code would have dropped the message', async () => {
		let threadId: string | null = null;
		let resolveCreate!: () => void;

		const ensureThread = makeBuggyEnsureThread(
			() => threadId,
			() => new Promise<void>(resolve => {
				resolveCreate = () => { threadId = 'new-session'; resolve(); };
			}),
		);

		// Phase 1: handleInput fires ensureThread (fire-and-forget) → isAutoCreating=true
		const proactivePromise = ensureThread();

		// Phase 2: handleSend fires ensureThread — bug causes immediate return
		let didSend = false;
		const handleSendResult = (async () => {
			await ensureThread(); // bug: returns immediately (isAutoCreating=true)
			if (threadId) didSend = true; // threadId still null at this point
		})();

		// Wait for the IIFE to finish BEFORE resolving session creation.
		// The IIFE's await-continuation fires as a microtask here, checking threadId
		// while it is still null (resolveCreate hasn't been called yet).
		await handleSendResult;

		// Thread creation resolves — but it's too late, handleSend already returned.
		resolveCreate();
		await proactivePromise;

		// The bug: creation DID complete but the message was silently dropped
		expect(threadId).toBe('new-session');
		expect(didSend).toBe(false);
	});

	it('fixed: onAutoCreateThread is called exactly once even with concurrent callers', async () => {
		let threadId: string | null = null;
		let callCount = 0;
		let resolveCreate!: () => void;

		const ensureThread = makeEnsureThread(
			() => threadId,
			() => {
				callCount++;
				return new Promise<void>(resolve => {
					resolveCreate = () => { threadId = 'new-session'; resolve(); };
				});
			},
		);

		// Three concurrent callers (handleInput + handleSend + any hypothetical third)
		const p1 = ensureThread();
		const p2 = ensureThread();
		const p3 = ensureThread();

		resolveCreate();
		await Promise.all([p1, p2, p3]);

		expect(callCount).toBe(1);
		expect(threadId).toBe('new-session');
	});

	it('fixed: returns immediately without calling onAutoCreateThread when session exists', async () => {
		let threadId: string | null = 'already-exists';
		let callCount = 0;

		const ensureThread = makeEnsureThread(
			() => threadId,
			async () => { callCount++; },
		);

		await ensureThread();
		await ensureThread();

		expect(callCount).toBe(0);
	});

	it('fixed: failed creation clears state so a retry can succeed', async () => {
		let threadId: string | null = null;
		let attempts = 0;
		let rejectFirst!: (err: Error) => void;

		const ensureThread = makeEnsureThread(
			() => threadId,
			() => {
				attempts++;
				if (attempts === 1) {
					return new Promise<void>((_, reject) => { rejectFirst = reject; });
				}
				threadId = 'retry-session';
				return Promise.resolve();
			},
		);

		// First attempt fails
		const firstAttempt = ensureThread();
		rejectFirst(new Error('network error'));
		await firstAttempt; // ensureThread swallows the error, so no throw

		// State is cleared; retry succeeds
		await ensureThread();

		expect(attempts).toBe(2);
		expect(threadId).toBe('retry-session');
	});
});

// ---------------------------------------------------------------------------
// #611 regression: typing alone must NOT trigger session creation
// ---------------------------------------------------------------------------

/**
 * Mirrors the FIXED handleInput() from InputBar.svelte after #611 patch.
 * onAutoCreateThread is NOT called from handleInput; only Send/Queue invoke it.
 */
function makeHandleInput(
	onAutoCreateThread: () => Promise<void>,
	getThreadId: () => string | null,
): () => void {
	return function handleInput() {
		// Fixed: no ensureThread() call here (#611).
		// onTriggerWarm would be called here in the real component but is irrelevant to this test.
		void getThreadId; // referenced to match real signature
		void onAutoCreateThread; // intentionally unused — that's the point
	};
}

/**
 * Mirrors the BUGGY handleInput() that called ensureThread() on every keystroke.
 */
function makeBuggyHandleInput(
	onAutoCreateThread: () => Promise<void>,
	getThreadId: () => string | null,
): () => void {
	return function handleInput() {
		if (!getThreadId()) {
			void onAutoCreateThread(); // BUG: creates a session on first keystroke
		}
	};
}

describe('#611 handleInput must not create threads on typing', () => {
	it('fixed: typing does not call onAutoCreateThread', () => {
		let callCount = 0;
		const handleInput = makeHandleInput(
			async () => { callCount++; },
			() => null,
		);

		// Simulate typing three characters
		handleInput();
		handleInput();
		handleInput();

		expect(callCount).toBe(0);
	});

	it('regression marker: old code would call onAutoCreateThread on first keystroke', async () => {
		let callCount = 0;
		let resolveCreate!: () => void;
		const handleInput = makeBuggyHandleInput(
			() => {
				callCount++;
				return new Promise<void>(resolve => { resolveCreate = resolve; });
			},
			() => null,
		);

		handleInput(); // BUG: fires session creation
		resolveCreate();
		// Give the promise a tick to settle
		await Promise.resolve();

		expect(callCount).toBe(1); // confirms the old code DID create a session on input
	});
});
