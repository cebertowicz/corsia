/**
 * The pure Turn state machine: one thread's client-visible turn state,
 * and the actions that move it. No store, no I/O — the wiring lives in
 * `turnState.ts`, which re-exports everything here.
 */
export type TurnPhase = 'idle' | 'requested' | 'active' | 'terminal';

export interface TurnIdentity {
	turnId: string;
	turnOrder: number;
}

export interface ThreadTurnState {
	turnId: string | null;
	turnOrder: number | null;
	phase: TurnPhase;
	pendingInputIds: readonly string[];
	/**
	 * The server-named topmost visible (spawn-free) ancestor of the thread
	 * this Turn runs on, or null when unknown. Lineage, not phase: it never
	 * influences the state machine, only which visible row may show a
	 * descendant's motion (see `descendantRunning`).
	 */
	visibleRootThreadId: string | null;
	/**
	 * When the running Turn was durably admitted, in epoch milliseconds, or null
	 * when no admitted frame has named it. It belongs to the Turn: a successor
	 * replaces it and the Turn ending drops it.
	 */
	startedAt: number | null;
}

export const INITIAL_TURN_STATE: ThreadTurnState = Object.freeze({
	turnId: null,
	turnOrder: null,
	phase: 'idle',
	pendingInputIds: Object.freeze([]) as readonly string[],
	visibleRootThreadId: null,
	startedAt: null,
});

export type TurnAction =
	| { type: 'input_requested'; inputId: string }
	| { type: 'input_accepted'; inputId: string; turnId: string; turnOrder: number }
	| { type: 'input_rejected'; inputId: string }
	| { type: 'input_recovered'; inputId: string }
	| { type: 'auth_restore'; identity: TurnIdentity | null; visibleRootThreadId?: string | null; startedAt?: number | null }
	| { type: 'snapshot'; turnId: string | null; turnOrder: number | null; visibleRootThreadId?: string | null; startedAt?: number | null }
	| { type: 'lifecycle'; turnId: string; turnOrder: number; visibleRootThreadId?: string | null; startedAt?: number | null }
	| { type: 'terminal'; turnId: string; turnOrder: number }
	| { type: 'reset' };

export type LifecycleDecision = 'none' | 'ignored' | 'same' | 'successor';

export interface TurnTransition {
	next: ThreadTurnState;
	lifecycle: LifecycleDecision;
	terminalApplied: boolean;
	inputResolved: boolean;
	previousIdentity: TurnIdentity | null;
}

function identityOf(state: ThreadTurnState): TurnIdentity | null {
	return state.turnId !== null && state.turnOrder !== null
		? { turnId: state.turnId, turnOrder: state.turnOrder }
		: null;
}

/**
 * The lineage for an already-recorded Turn: an undefined root (a frame that
 * does not carry the field) keeps what is recorded; a definite value,
 * including null, is server truth and replaces it.
 */
function nextRoot(
	state: ThreadTurnState,
	visibleRootThreadId: string | null | undefined,
): string | null {
	return visibleRootThreadId === undefined ? state.visibleRootThreadId : visibleRootThreadId;
}

/**
 * The start for an already-recorded Turn. A Turn's admission instant never
 * changes, so a frame without one (absent, or a server read that degraded to
 * null) keeps what is recorded.
 */
function nextStart(state: ThreadTurnState, startedAt: number | null | undefined): number | null {
	return startedAt ?? state.startedAt;
}

function transition(
	next: ThreadTurnState,
	options: Partial<Omit<TurnTransition, 'next'>> = {},
): TurnTransition {
	return {
		next,
		lifecycle: options.lifecycle ?? 'none',
		terminalApplied: options.terminalApplied ?? false,
		inputResolved: options.inputResolved ?? false,
		previousIdentity: options.previousIdentity ?? null,
	};
}

function compareIdentity(
	state: ThreadTurnState,
	turnId: string,
	turnOrder: number,
): 'new' | 'lower' | 'same' | 'collision' {
	if (state.turnOrder === null || state.turnId === null) return 'new';
	if (turnOrder < state.turnOrder) return 'lower';
	if (turnOrder > state.turnOrder) return 'new';
	return turnId === state.turnId ? 'same' : 'collision';
}

function withoutPending(state: ThreadTurnState, inputId: string): {
	next: ThreadTurnState;
	removed: boolean;
} {
	if (!state.pendingInputIds.includes(inputId)) {
		return { next: state, removed: false };
	}
	const pendingInputIds = state.pendingInputIds.filter(id => id !== inputId);
	const phase = (
		state.turnId === null && pendingInputIds.length === 0 && state.phase === 'requested'
	) ? 'idle' : state.phase;
	return {
		next: { ...state, phase, pendingInputIds },
		removed: true,
	};
}

function adoptActive(
	state: ThreadTurnState,
	turnId: string,
	turnOrder: number,
	visibleRootThreadId?: string | null,
	startedAt?: number | null,
): TurnTransition {
	const comparison = compareIdentity(state, turnId, turnOrder);
	if (comparison === 'lower' || comparison === 'collision') {
		return transition(state, { lifecycle: 'ignored' });
	}
	if (comparison === 'same') {
		if (state.phase === 'terminal') {
			return transition(state, { lifecycle: 'same' });
		}
		const root = nextRoot(state, visibleRootThreadId);
		const start = nextStart(state, startedAt);
		return transition(
			state.phase === 'active' && state.visibleRootThreadId === root && state.startedAt === start
				? state
				: { ...state, phase: 'active', visibleRootThreadId: root, startedAt: start },
			{ lifecycle: 'same' },
		);
	}
	const previousIdentity = identityOf(state);
	return transition({
		...state,
		turnId,
		turnOrder,
		phase: 'active',
		visibleRootThreadId: visibleRootThreadId ?? null,
		startedAt: startedAt ?? null,
	}, {
		lifecycle: 'successor',
		previousIdentity,
	});
}

/**
 * Settle a thread the server reports no live turn for.
 *
 * Only an `active` phase changes: a `requested` phase is an input this client
 * is still waiting on an answer for, and a pending correlation is not the
 * server's to close.
 */
function settleWithoutServerTurn(state: ThreadTurnState): TurnTransition {
	if (state.phase !== 'active') return transition(state);
	return transition({ ...state, phase: 'terminal', startedAt: null }, {
		lifecycle: 'same',
		terminalApplied: true,
	});
}

export function reduceTurnState(
	state: ThreadTurnState,
	action: TurnAction,
): TurnTransition {
	switch (action.type) {
		case 'input_requested': {
			if (state.pendingInputIds.includes(action.inputId)) return transition(state);
			return transition({
				...state,
				phase: state.turnId === null ? 'requested' : state.phase,
				pendingInputIds: [...state.pendingInputIds, action.inputId],
			});
		}
		case 'input_rejected':
		case 'input_recovered': {
			const { next, removed } = withoutPending(state, action.inputId);
			return transition(next, { inputResolved: removed });
		}
		case 'input_accepted': {
			const { next: correlated, removed } = withoutPending(state, action.inputId);
			const adopted = adoptActive(correlated, action.turnId, action.turnOrder);
			return { ...adopted, inputResolved: removed };
		}
		case 'lifecycle':
			return adoptActive(
				state, action.turnId, action.turnOrder, action.visibleRootThreadId, action.startedAt,
			);
		case 'snapshot':
			// A snapshot with no identity is the server saying this thread has no
			// live turn (`ws/EVENTS.md`: both null when inactive), so it settles an
			// active phase exactly as `auth_restore` does. Treating it as
			// non-authoritative is how a turn that ended while the socket was away
			// left a thread "active" forever: a spinning tool block and a Stop
			// button on a turn that was durably interrupted minutes earlier.
			if (action.turnId === null || action.turnOrder === null) {
				return settleWithoutServerTurn(state);
			}
			return adoptActive(
				state, action.turnId, action.turnOrder, action.visibleRootThreadId, action.startedAt,
			);
		case 'terminal': {
			const comparison = compareIdentity(state, action.turnId, action.turnOrder);
			if (comparison === 'lower' || comparison === 'collision') {
				return transition(state, { lifecycle: 'ignored' });
			}
			if (comparison === 'same' && state.phase === 'terminal') {
				return transition(state, { lifecycle: 'same' });
			}
			const previousIdentity = comparison === 'new' ? identityOf(state) : null;
			return transition({
				...state,
				turnId: action.turnId,
				turnOrder: action.turnOrder,
				phase: 'terminal',
				startedAt: null,
			}, {
				lifecycle: comparison === 'new' ? 'successor' : 'same',
				terminalApplied: true,
				previousIdentity,
			});
		}
		case 'auth_restore':
			if (action.identity === null) return settleWithoutServerTurn(state);
			{
				const sameIdentity = (
					state.turnId === action.identity.turnId
					&& state.turnOrder === action.identity.turnOrder
				);
				if (sameIdentity) {
					const visibleRootThreadId = nextRoot(state, action.visibleRootThreadId);
					const startedAt = nextStart(state, action.startedAt);
					return transition(
						state.phase === 'active'
							&& state.visibleRootThreadId === visibleRootThreadId && state.startedAt === startedAt
							? state
							: { ...state, phase: 'active', visibleRootThreadId, startedAt },
						{ lifecycle: 'same' },
					);
				}
				return transition({
					...state,
					turnId: action.identity.turnId,
					turnOrder: action.identity.turnOrder,
					phase: 'active',
					visibleRootThreadId: action.visibleRootThreadId ?? null,
					startedAt: action.startedAt ?? null,
				}, {
					lifecycle: 'successor',
					previousIdentity: identityOf(state),
				});
			}
		case 'reset':
			return transition(INITIAL_TURN_STATE);
	}
}
