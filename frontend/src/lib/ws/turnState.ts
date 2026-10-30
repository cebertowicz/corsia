import { get, writable, type Readable } from 'svelte/store';
import {
	INITIAL_TURN_STATE,
	reduceTurnState,
	type ThreadTurnState,
	type TurnAction,
	type TurnIdentity,
	type TurnTransition,
} from './turnStateReducer';

// The pure reducer and its types stay importable from here, the one module
// the app already wires to.
export {
	INITIAL_TURN_STATE,
	reduceTurnState,
} from './turnStateReducer';
export type {
	LifecycleDecision,
	ThreadTurnState,
	TurnAction,
	TurnIdentity,
	TurnPhase,
	TurnTransition,
} from './turnStateReducer';

const mutableTurnStates = writable<ReadonlyMap<string, ThreadTurnState>>(new Map());

export const turnStates: Readable<ReadonlyMap<string, ThreadTurnState>> = {
	subscribe: mutableTurnStates.subscribe,
};

function dispatch(threadId: string, action: TurnAction): TurnTransition {
	const current = get(mutableTurnStates).get(threadId) ?? INITIAL_TURN_STATE;
	const result = reduceTurnState(current, action);
	if (result.next !== current) {
		mutableTurnStates.update(states => {
			const next = new Map(states);
			if (result.next === INITIAL_TURN_STATE) next.delete(threadId);
			else next.set(threadId, result.next);
			return next;
		});
	}
	return result;
}

export function getTurnState(threadId: string): ThreadTurnState {
	return get(mutableTurnStates).get(threadId) ?? INITIAL_TURN_STATE;
}

export function requestInput(threadId: string, inputId: string): TurnTransition {
	return dispatch(threadId, { type: 'input_requested', inputId });
}

export function acceptInput(
	threadId: string,
	inputId: string,
	identity: TurnIdentity,
): TurnTransition {
	return dispatch(threadId, {
		type: 'input_accepted', inputId,
		turnId: identity.turnId, turnOrder: identity.turnOrder,
	});
}

export function rejectInput(threadId: string, inputId: string): TurnTransition {
	return dispatch(threadId, { type: 'input_rejected', inputId });
}

export function recoverInput(threadId: string, inputId: string): TurnTransition {
	return dispatch(threadId, { type: 'input_recovered', inputId });
}

export function applyLifecycle(
	threadId: string,
	identity: TurnIdentity,
	visibleRootThreadId?: string | null,
	startedAt?: number | null,
): TurnTransition {
	return dispatch(threadId, {
		type: 'lifecycle', turnId: identity.turnId, turnOrder: identity.turnOrder,
		visibleRootThreadId, startedAt,
	});
}

export function applyTerminal(
	threadId: string,
	identity: TurnIdentity,
): TurnTransition {
	return dispatch(threadId, {
		type: 'terminal', turnId: identity.turnId,
		turnOrder: identity.turnOrder,
	});
}

export function applySnapshot(
	threadId: string,
	identity: TurnIdentity | null,
	visibleRootThreadId?: string | null,
	startedAt?: number | null,
): TurnTransition {
	return dispatch(threadId, {
		type: 'snapshot',
		turnId: identity?.turnId ?? null,
		turnOrder: identity?.turnOrder ?? null,
		visibleRootThreadId,
		startedAt,
	});
}

export function restoreAuthTurns(
	activeTurns: ReadonlyArray<TurnIdentity & {
		threadId: string;
		visibleRootThreadId?: string | null;
		startedAt?: number | null;
	}>,
): ReadonlyMap<string, TurnTransition> {
	const activeByThread = new Map(activeTurns.map(turn => [turn.threadId, turn]));
	const threadIds = new Set([
		...get(mutableTurnStates).keys(),
		...activeByThread.keys(),
	]);
	const results = new Map<string, TurnTransition>();
	for (const threadId of threadIds) {
		const active = activeByThread.get(threadId);
		results.set(threadId, dispatch(threadId, {
			type: 'auth_restore',
			identity: active
				? { turnId: active.turnId, turnOrder: active.turnOrder }
				: null,
			// Snapshot entries name their root; a definite null (unknown lineage)
			// is server truth, while an absent entry settles instead.
			visibleRootThreadId: active ? (active.visibleRootThreadId ?? null) : undefined,
			startedAt: active?.startedAt,
		}));
	}
	return results;
}

export function resetTurnState(threadId: string): TurnTransition {
	return dispatch(threadId, { type: 'reset' });
}

export function restoreTurnState(
	threadId: string,
	state: ThreadTurnState,
): void {
	mutableTurnStates.update(states => {
		const next = new Map(states);
		if (state === INITIAL_TURN_STATE) next.delete(threadId);
		else next.set(threadId, state);
		return next;
	});
}

export function hasActiveTurn(threadId: string): boolean {
	return getTurnState(threadId).phase === 'active';
}

/**
 * The visible thread a running descendant Turn rolls up to (its spawned_by
 * root, named by the server), or null when the Turn is idle or runs on the
 * visible thread itself. The sidebar spinner and the work block both read this.
 */
export function rolledUpRoot(threadId: string, state: ThreadTurnState): string | null {
	if (state.phase !== 'active' || state.visibleRootThreadId === threadId) return null;
	return state.visibleRootThreadId;
}

export function acknowledgedTurnId(threadId: string): string | null {
	const state = getTurnState(threadId);
	return state.phase === 'active' ? state.turnId : null;
}

export function isCurrentTurn(
	threadId: string,
	identity: TurnIdentity,
): boolean {
	const state = getTurnState(threadId);
	return state.turnId === identity.turnId && state.turnOrder === identity.turnOrder;
}

export function pendingInputThreads(): Array<{
	threadId: string;
	inputIds: readonly string[];
}> {
	return [...get(mutableTurnStates)].flatMap(([threadId, state]) =>
		state.pendingInputIds.length > 0
			? [{ threadId, inputIds: state.pendingInputIds }]
			: []
	);
}

export function _resetTurnStateForTests(): void {
	mutableTurnStates.set(new Map());
}
