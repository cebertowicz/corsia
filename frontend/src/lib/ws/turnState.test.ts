import { describe, expect, it } from 'vitest';

import {
	INITIAL_TURN_STATE,
	reduceTurnState,
	type ThreadTurnState,
} from './turnState';

function reduce(
	state: ThreadTurnState,
	action: Parameters<typeof reduceTurnState>[1],
): ReturnType<typeof reduceTurnState> {
	return reduceTurnState(state, action);
}

describe('turnState reducer', () => {
	it('keeps C authoritative through adversarial P -> A -> B -> C delivery', () => {
		let state = INITIAL_TURN_STATE;
		state = reduce(state, { type: 'input_requested', inputId: 'P' }).next;
		state = reduce(state, { type: 'input_requested', inputId: 'A' }).next;
		state = reduce(state, {
			type: 'input_accepted', inputId: 'A', turnId: 'turn-A', turnOrder: 10,
		}).next;
		state = reduce(state, { type: 'lifecycle', turnId: 'turn-B', turnOrder: 11 }).next;
		state = reduce(state, {
			type: 'terminal', turnId: 'turn-B', turnOrder: 11,
		}).next;
		state = reduce(state, { type: 'lifecycle', turnId: 'turn-C', turnOrder: 12 }).next;

		const staleTerminal = reduce(state, {
			type: 'terminal', turnId: 'turn-A', turnOrder: 10,
		});
		expect(staleTerminal.terminalApplied).toBe(false);
		expect(staleTerminal.lifecycle).toBe('ignored');
		state = staleTerminal.next;

		const lateAcceptance = reduce(state, {
			type: 'input_accepted', inputId: 'P', turnId: 'turn-A', turnOrder: 10,
		});
		expect(lateAcceptance.inputResolved).toBe(true);
		expect(lateAcceptance.lifecycle).toBe('ignored');
		expect(lateAcceptance.next).toMatchObject({
			turnId: 'turn-C', turnOrder: 12, phase: 'active',
		});
	});

	it('applies a terminal once and a late same-turn acceptance cannot resurrect it', () => {
		let state = reduce(INITIAL_TURN_STATE, {
			type: 'terminal', turnId: 'turn-A', turnOrder: 1,
		}).next;
		const duplicate = reduce(state, {
			type: 'terminal', turnId: 'turn-A', turnOrder: 1,
		});
		expect(duplicate.terminalApplied).toBe(false);

		state = reduce(duplicate.next, { type: 'input_requested', inputId: 'input-A' }).next;
		const lateAck = reduce(state, {
			type: 'input_accepted', inputId: 'input-A', turnId: 'turn-A', turnOrder: 1,
		});
		expect(lateAck.lifecycle).toBe('same');
		expect(lateAck.next.phase).toBe('terminal');
		expect(lateAck.next.pendingInputIds).toEqual([]);
	});

	it('preserves same-ID Codex state and identifies a different higher-ID successor', () => {
		const active = reduce(INITIAL_TURN_STATE, {
			type: 'lifecycle', turnId: 'turn-A', turnOrder: 3,
		}).next;
		const requested = reduce(active, { type: 'input_requested', inputId: 'steer' }).next;
		const steer = reduce(requested, {
			type: 'input_accepted', inputId: 'steer', turnId: 'turn-A', turnOrder: 3,
		});
		expect(steer.lifecycle).toBe('same');
		expect(steer.next).toMatchObject({ turnId: 'turn-A', turnOrder: 3, phase: 'active' });

		const successor = reduce(steer.next, { type: 'input_requested', inputId: 'replace' });
		const accepted = reduce(successor.next, {
			type: 'input_accepted', inputId: 'replace', turnId: 'turn-B', turnOrder: 4,
		});
		expect(accepted.lifecycle).toBe('successor');
		expect(accepted.previousIdentity).toEqual({ turnId: 'turn-A', turnOrder: 3 });
		expect(accepted.next).toMatchObject({
			turnId: 'turn-B', turnOrder: 4, phase: 'active',
		});
	});

	it('rejects only the correlated pending input and preserves its predecessor', () => {
		let state = reduce(INITIAL_TURN_STATE, {
			type: 'lifecycle', turnId: 'turn-A', turnOrder: 7,
		}).next;
		state = reduce(state, { type: 'input_requested', inputId: 'one' }).next;
		state = reduce(state, { type: 'input_requested', inputId: 'two' }).next;
		const rejected = reduce(state, { type: 'input_rejected', inputId: 'one' });
		expect(rejected.inputResolved).toBe(true);
		expect(rejected.next).toEqual({
			turnId: 'turn-A',
			turnOrder: 7,
			phase: 'active',
			pendingInputIds: ['two'],
			visibleRootThreadId: null,
			startedAt: null,
		});
	});

	it('lost-ack recovery resolves correlation without inventing turn identity', () => {
		const requested = reduce(INITIAL_TURN_STATE, {
			type: 'input_requested', inputId: 'lost-ack',
		}).next;
		const recovered = reduce(requested, {
			type: 'input_recovered', inputId: 'lost-ack',
		});
		expect(recovered.inputResolved).toBe(true);
		expect(recovered.next).toEqual(INITIAL_TURN_STATE);
	});

	it('settles an active turn when a snapshot reports no live turn', () => {
		// `session_snapshot` carries both ids null when the thread is inactive
		// (ws/EVENTS.md), so it is the server saying "nothing is running here".
		// Treating it as non-authoritative is how a turn that ended while the
		// socket was away left a thread spinning with a Stop button on a turn
		// that was durably interrupted minutes earlier (USER-REPORTS #6).
		const active = reduce(INITIAL_TURN_STATE, {
			type: 'lifecycle', turnId: 'turn-B', turnOrder: 22,
		}).next;
		const snapshot = reduce(active, {
			type: 'snapshot', turnId: null, turnOrder: null,
		});
		expect(snapshot.terminalApplied).toBe(true);
		expect(snapshot.next).toMatchObject({
			turnId: 'turn-B', turnOrder: 22, phase: 'terminal',
		});
	});

	it('leaves an unanswered input pending when a snapshot reports no live turn', () => {
		// A `requested` phase is a correlation this client is still owed. The
		// server having no turn yet is not an answer to it.
		const requested = reduce(INITIAL_TURN_STATE, {
			type: 'input_requested', inputId: 'in-flight',
		}).next;
		const snapshot = reduce(requested, {
			type: 'snapshot', turnId: null, turnOrder: null,
		});
		expect(snapshot.next).toBe(requested);
		expect(snapshot.terminalApplied).toBe(false);
	});

	it('auth restoration terminalizes omitted active turns without erasing scalar order', () => {
		const active = reduce(INITIAL_TURN_STATE, {
			type: 'lifecycle', turnId: 'turn-B', turnOrder: 22,
		}).next;
		const absent = reduce(active, { type: 'auth_restore', identity: null });
		expect(absent.terminalApplied).toBe(true);
		expect(absent.next).toMatchObject({
			turnId: 'turn-B', turnOrder: 22, phase: 'terminal',
		});

	});

	it('auth restoration authoritatively replaces stale local order and terminal phase', () => {
		const staleLocal = reduce(INITIAL_TURN_STATE, {
			type: 'terminal', turnId: 'stale-local', turnOrder: 99,
		}).next;
		const lowerServerTruth = reduce(staleLocal, {
			type: 'auth_restore', identity: { turnId: 'server-active', turnOrder: 2 },
		});
		expect(lowerServerTruth.lifecycle).toBe('successor');
		expect(lowerServerTruth.previousIdentity).toEqual({
			turnId: 'stale-local', turnOrder: 99,
		});
		expect(lowerServerTruth.next).toMatchObject({
			turnId: 'server-active', turnOrder: 2, phase: 'active',
		});

		const localTerminal = reduce(INITIAL_TURN_STATE, {
			type: 'terminal', turnId: 'same-turn', turnOrder: 5,
		}).next;
		const sameServerTruth = reduce(localTerminal, {
			type: 'auth_restore', identity: { turnId: 'same-turn', turnOrder: 5 },
		});
		expect(sameServerTruth.lifecycle).toBe('same');
		expect(sameServerTruth.next).toMatchObject({ phase: 'active' });
	});

	it('reset is the only action that clears the monotonic scalar', () => {
		let state = reduce(INITIAL_TURN_STATE, {
			type: 'terminal', turnId: 'turn-Z', turnOrder: 99,
		}).next;
		expect(state).toMatchObject({ turnId: 'turn-Z', turnOrder: 99 });
		expect(reduce(state, { type: 'reset' }).next).toEqual(INITIAL_TURN_STATE);
	});
	describe('root lineage (descendant motion truth)', () => {
		it('records the visible root a turn_activity frame names', () => {
			const adopted = reduce(INITIAL_TURN_STATE, {
				type: 'lifecycle', turnId: 'turn-cc', turnOrder: 4, visibleRootThreadId: 'parent-1',
			});
			expect(adopted.next.visibleRootThreadId).toBe('parent-1');
		});

		it('keeps the recorded root when a frame without the field re-admits the same turn', () => {
			// Streaming frames (tool_start, deltas) carry identity but not
			// lineage; they must not erase the root turn_activity recorded.
			let state = reduce(INITIAL_TURN_STATE, {
				type: 'lifecycle', turnId: 'turn-cc', turnOrder: 4, visibleRootThreadId: 'parent-1',
			}).next;
			state = reduce(state, {
				type: 'lifecycle', turnId: 'turn-cc', turnOrder: 4,
			}).next;
			expect(state.visibleRootThreadId).toBe('parent-1');
		});

		it('replaces the root when the server sends a definite value, including null', () => {
			let state = reduce(INITIAL_TURN_STATE, {
				type: 'lifecycle', turnId: 'turn-cc', turnOrder: 4, visibleRootThreadId: 'parent-1',
			}).next;
			state = reduce(state, {
				type: 'lifecycle', turnId: 'turn-cc', turnOrder: 4, visibleRootThreadId: null,
			}).next;
			expect(state.visibleRootThreadId).toBeNull();
		});

		it('resets lineage for a successor turn that carries none', () => {
			let state = reduce(INITIAL_TURN_STATE, {
				type: 'lifecycle', turnId: 'turn-old', turnOrder: 4, visibleRootThreadId: 'parent-1',
			}).next;
			state = reduce(state, {
				type: 'input_accepted', inputId: 'in-1', turnId: 'turn-new', turnOrder: 5,
			}).next;
			expect(state).toMatchObject({ turnId: 'turn-new', phase: 'active' });
			expect(state.visibleRootThreadId).toBeNull();
		});

		it('records the root an auth snapshot names, on the same identity', () => {
			let state = reduce(INITIAL_TURN_STATE, {
				type: 'lifecycle', turnId: 'turn-B', turnOrder: 22,
			}).next;
			state = reduce(state, {
				type: 'auth_restore',
				identity: { turnId: 'turn-B', turnOrder: 22 },
				visibleRootThreadId: 'parent-1',
			}).next;
			expect(state.visibleRootThreadId).toBe('parent-1');
		});

		it('carries the root through a terminal so cleanup keys on the thread, not the field', () => {
			// The terminal frame needs no root field: the entry is keyed by its
			// own thread and the phase alone retires descendant motion.
			let state = reduce(INITIAL_TURN_STATE, {
				type: 'lifecycle', turnId: 'turn-cc', turnOrder: 4, visibleRootThreadId: 'parent-1',
			}).next;
			state = reduce(state, {
				type: 'terminal', turnId: 'turn-cc', turnOrder: 4,
			}).next;
			expect(state).toMatchObject({ phase: 'terminal' });
			expect(state.visibleRootThreadId).toBe('parent-1');
		});
	});

	describe('turn start (belongs to the Turn)', () => {
		const active = (startedAt?: number | null) => reduce(INITIAL_TURN_STATE, {
			type: 'lifecycle', turnId: 'turn-B', turnOrder: 5, startedAt,
		}).next;

		it('records the start an admitted frame names, and keeps it through frames without one', () => {
			let state = active(1_000);
			expect(state.startedAt).toBe(1_000);
			state = reduce(state, { type: 'lifecycle', turnId: 'turn-B', turnOrder: 5 }).next;
			expect(state.startedAt).toBe(1_000);
			state = reduce(state, { type: 'lifecycle', turnId: 'turn-B', turnOrder: 5, startedAt: null }).next;
			expect(state.startedAt).toBe(1_000);
		});

		it('fills a start the Turn was admitted without', () => {
			const state = reduce(active(), {
				type: 'snapshot', turnId: 'turn-B', turnOrder: 5, startedAt: 2_000,
			}).next;
			expect(state.startedAt).toBe(2_000);
		});

		it('ignores the start on a stale or colliding snapshot', () => {
			const state = active(1_000);
			const stale = reduce(state, { type: 'snapshot', turnId: 'turn-A', turnOrder: 4, startedAt: 9_000 });
			expect(stale.lifecycle).toBe('ignored');
			expect(stale.next.startedAt).toBe(1_000);
			const collision = reduce(state, { type: 'snapshot', turnId: 'turn-X', turnOrder: 5, startedAt: 9_000 });
			expect(collision.lifecycle).toBe('ignored');
			expect(collision.next.startedAt).toBe(1_000);
		});

		it('replaces the start with a newer Turn\'s, or drops it when that Turn names none', () => {
			const newer = reduce(active(1_000), {
				type: 'lifecycle', turnId: 'turn-C', turnOrder: 6, startedAt: 3_000,
			});
			expect(newer.lifecycle).toBe('successor');
			expect(newer.next.startedAt).toBe(3_000);
			const unnamed = reduce(active(1_000), {
				type: 'input_accepted', inputId: 'in-1', turnId: 'turn-C', turnOrder: 6,
			}).next;
			expect(unnamed.startedAt).toBeNull();
		});

		it('drops the start when the Turn ends, by terminal or by server truth', () => {
			const terminal = reduce(active(1_000), {
				type: 'terminal', turnId: 'turn-B', turnOrder: 5,
			}).next;
			expect(terminal).toMatchObject({ phase: 'terminal', startedAt: null });
			const settled = reduce(active(1_000), { type: 'snapshot', turnId: null, turnOrder: null }).next;
			expect(settled).toMatchObject({ phase: 'terminal', startedAt: null });
			const restored = reduce(active(1_000), { type: 'auth_restore', identity: null }).next;
			expect(restored).toMatchObject({ phase: 'terminal', startedAt: null });
		});

		it('takes the start an auth snapshot names for a new or the same Turn', () => {
			const same = reduce(active(), {
				type: 'auth_restore', identity: { turnId: 'turn-B', turnOrder: 5 }, startedAt: 4_000,
			}).next;
			expect(same.startedAt).toBe(4_000);
			const fresh = reduce(INITIAL_TURN_STATE, {
				type: 'auth_restore', identity: { turnId: 'turn-B', turnOrder: 5 }, startedAt: 4_000,
			}).next;
			expect(fresh.startedAt).toBe(4_000);
		});
	});
});
