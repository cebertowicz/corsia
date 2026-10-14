import { get } from 'svelte/store';
import { isMachineCode, plainErrorText } from '../plainError';
import { token, user } from '../stores/auth';
import {
	activeThreadId,
	messages,
	clearThreadBuffers,
	type Message,
} from '../stores/threads';
import {
	applyDrainStatus,
	connectionState,
	connectionError,
	refreshDrainStatus,
} from '../stores/connection';
import { isDisplayedThread } from '../stores/displayedThreads';
import { clearAllContextUsage } from '../stores/contextUsageByThread';
import { loadUpdateStatus } from '../update/store';
import { emit } from './events';
import { subscribedThreads } from './state';
import { resubscribeWatchedThreads, stopRequestedTurns, subscribeThread } from './send';
import { cancelThreadTypewriters } from './typewriter';
import { frameIdentity, frameStartedAt } from './turnFrames';
import {
	handleInputRejected,
	handleTurnError,
} from './handlersTurns';
import { recoverPendingInputs } from './pendingRecovery';
import { restoreAuthTurns, type TurnIdentity } from './turnState';

// `isCurrentOwner` is the delivering socket's ownership check. The body below
// runs synchronously under that owner; the detached reads re-check it after
// every await, so a retired socket's reads never write into current state.
export function handleAuthOk(
	data: any,
	resetHeartbeat: () => void,
	isCurrentOwner: () => boolean = () => true,
) {
	user.set(data.user);
	clearAllContextUsage();
	connectionState.set('connected');
	connectionError.set(null);
	applyDrainStatus({ active: false });
	const authToken = get(token);
	if (authToken) {
		void refreshDrainStatus(authToken, isCurrentOwner).catch((error) => {
			console.warn('[drain] initial status refresh failed', error);
		});
	}
	resetHeartbeat();
	const activeTurns = (data.active_turns || []).flatMap((turn: any) =>
		typeof turn?.thread_id === 'string'
		&& typeof turn?.turn_id === 'string'
		&& Number.isInteger(turn?.turn_order)
			? [{
				threadId: turn.thread_id,
				turnId: turn.turn_id,
				turnOrder: turn.turn_order,
				// The snapshot names each running Turn's visible root; a missing
				// field is an unknown lineage (no descendant motion), not an error.
				visibleRootThreadId: typeof turn?.visible_root_thread_id === 'string'
					? turn.visible_root_thread_id
					: null,
				startedAt: frameStartedAt(turn),
			}]
			: []
	);
	const transitions = restoreAuthTurns(activeTurns);
	for (const [threadId, transition] of transitions) {
		if (transition.lifecycle === 'successor' && transition.previousIdentity) {
			cancelThreadTypewriters(threadId, transition.previousIdentity);
			clearThreadBuffers(threadId);
		} else if (transition.terminalApplied) {
			const identity: TurnIdentity | null = (
				transition.next.turnId !== null && transition.next.turnOrder !== null
			) ? {
				turnId: transition.next.turnId,
				turnOrder: transition.next.turnOrder,
			} : null;
			if (identity) cancelThreadTypewriters(threadId, identity);
			clearThreadBuffers(threadId);
		}
	}
	subscribedThreads.clear();
	const currentThread = get(activeThreadId);
	if (currentThread) subscribeThread(currentThread);
	resubscribeWatchedThreads();
	if (activeTurns.length > 0) {
		emit('active_turns_restored', {
			thread_ids: activeTurns.map((turn: { threadId: string }) => turn.threadId),
		});
	}
	void recoverPendingInputs(fetch, isCurrentOwner);
	emit('connected', data.user);
}

export function handleSystemDraining(data: any) {
	applyDrainStatus({ ...data, active: data.draining === true });
	// A drain is how an update started on another device reaches this one: read the operation now.
	void loadUpdateStatus();
}

function appendVisibleError(data: any): void {
	if (!isDisplayedThread(data.thread_id)) return;
	// Provider errors are sentences; some of ours are identifiers. An identifier
	// in the transcript asks the user to act on evidence they do not hold.
	if (typeof data.message === 'string' && isMachineCode(data.message)) {
		console.error('[ws] error frame', data.message, data);
	}
	const errMsg: Message = {
		id: crypto.randomUUID().slice(0, 8),
		thread_id: data.thread_id,
		role: 'system' as const,
		content: `Error: ${plainErrorText(data.message, 'something went wrong on CORSIA’s side')}`,
		metadata: data.error_type ? {
			type: data.error_type,
			event_type: data.event_type,
			harness: data.harness,
			cause: data.cause,
			repair_owner: data.repair_owner,
			continuity_mode: data.continuity_mode,
			model_context_window: data.model_context_window,
			total_tokens: data.total_tokens,
		} : undefined,
		created_at: new Date().toISOString(),
	};
	messages.update(current => [...current, errMsg]);
}

export function handleWsError(data: any) {
	if (
		typeof data?.thread_id === 'string'
		&& typeof data?.turn_id === 'string'
		&& data?.error_type === 'stop_failed'
	) {
		stopRequestedTurns.update(current => {
			if (current.get(data.thread_id) !== data.turn_id) return current;
			const next = new Map(current);
			next.delete(data.thread_id);
			return next;
		});
		appendVisibleError(data);
		emit('error', data);
		return;
	}
	if (data.message === 'Invalid or expired session') {
		token.set(null);
		user.set(null);
		connectionState.set('error');
		connectionError.set('Invalid or expired session');
		emit('error', data);
		return;
	}
	const identity = frameIdentity(data);
	if (typeof data?.input_id === 'string' && identity === null) {
		handleInputRejected(data);
		appendVisibleError(data);
		emit('error', data);
		return;
	}
	if (identity !== null) {
		if (handleTurnError(data)) appendVisibleError(data);
		emit('error', data);
		return;
	}
	// An uncorrelated error remains visible but cannot change lifecycle truth.
	appendVisibleError(data);
	emit('error', data);
}
