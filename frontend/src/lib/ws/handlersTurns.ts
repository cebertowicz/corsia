import { get } from 'svelte/store';
import {
	messages,
	streamContentByThread,
	liveReasoningByThread,
	setThreadReasoning,
	setThreadContent,
	setThreadActiveTools,
	updateThreadActiveTools,
	clearThreadBuffers,
	mergeMessageDelivery,
} from '../stores/threads';
import { settleActiveTool, startActiveTool } from '../stores/activeTools';
import { commitLiveStep } from '../stores/liveStep';
import { isDisplayedThread } from '../stores/displayedThreads';
import { recordMeasuredUsage, setContextUsage } from '../stores/contextUsageByThread';
import { readMessageDeliveryState } from '../messageDelivery';
import { compareMessageOrder } from '../messageHistory';
import { settleRebuildToast } from '../stores/warmToast';
import type { ActiveTool, Message } from '../stores/threads';
import { emit } from './events';
import { startTypewriter, cancelThreadTypewriters, isRevealing } from './typewriter';
import {
	applyDelta,
	beginPendingSnapshot,
	isPendingSnapshot,
	markSeqApplied,
	queuePending,
	takePendingDeltas,
} from './streamState';
import {
	acceptInput,
	applyLifecycle,
	applySnapshot,
	applyTerminal,
	getTurnState,
	recoverInput,
	rejectInput,
	type TurnIdentity,
	type TurnTransition,
} from './turnState';
import { frameIdentity, frameTurnFacts, stepTextMessage } from './turnFrames';

function resetPredecessor(
	threadId: string,
	transition: TurnTransition,
): void {
	if (transition.lifecycle !== 'successor' || !transition.previousIdentity) return;
	const awaitingSnapshot = isPendingSnapshot(threadId);
	cancelThreadTypewriters(threadId, transition.previousIdentity);
	clearThreadBuffers(threadId);
	setContextUsage(threadId, null);
	if (awaitingSnapshot) beginPendingSnapshot(threadId);
}

function applyPendingForIdentity(
	threadId: string,
	identity: TurnIdentity,
): void {
	for (const delta of takePendingDeltas(threadId, identity)) {
		applyDelta(delta, identity);
	}
}

function admitActiveFrame(data: any): TurnIdentity | null {
	const threadId = data?.thread_id;
	const identity = frameIdentity(data);
	if (!threadId || !identity) return null;
	const transition = applyLifecycle(threadId, identity, ...frameTurnFacts(data));
	if (transition.lifecycle === 'ignored' || transition.next.phase !== 'active') {
		return null;
	}
	resetPredecessor(threadId, transition);
	settleRebuildToast();
	return identity;
}

function admitTerminalFrame(data: any): TurnIdentity | null {
	const threadId = data?.thread_id;
	const identity = frameIdentity(data);
	if (!threadId || !identity) return null;
	const transition = applyTerminal(threadId, identity);
	if (!transition.terminalApplied) return null;
	resetPredecessor(threadId, transition);
	settleRebuildToast();
	return identity;
}

function reconcileOptimisticRow(
	threadId: string,
	inputId: string,
	messageId: string,
	createdAt: string,
	delivery: Pick<Message, 'delivery_state' | 'delivery_error' | 'delivery_revision' | 'seq'> = {},
): void {
	if (!isDisplayedThread(threadId)) return;
	messages.update(current => current.map(message =>
		(message.metadata?.input_id === inputId || message.id === inputId)
			? mergeMessageDelivery(message, {
				...message,
				id: messageId,
				created_at: createdAt,
				_optimistic: false,
				...delivery,
			})
			: message
	).sort(compareMessageOrder));
}

export function handleInputAccepted(data: any): void {
	const threadId = data?.thread_id;
	const inputId = data?.input_id;
	const messageId = data?.message_id;
	const createdAt = data?.created_at;
	const identity = frameIdentity(data);
	if (
		!threadId || typeof inputId !== 'string' || typeof messageId !== 'string'
		|| typeof createdAt !== 'string' || !identity
	) return;
	const transition = acceptInput(threadId, inputId, identity);
	reconcileOptimisticRow(threadId, inputId, messageId, createdAt, {
		delivery_state: 'delivered',
		delivery_error: null,
		...(typeof data.delivery_revision === 'number'
			? { delivery_revision: data.delivery_revision }
			: {}),
		...(typeof data.seq === 'number' && Number.isInteger(data.seq) && data.seq > 0
			? { seq: data.seq }
			: {}),
	});
	resetPredecessor(threadId, transition);
	emit('input_accepted', data);
}

export function handleInputRejected(data: any): boolean {
	const threadId = data?.thread_id;
	const inputId = data?.input_id;
	if (!threadId || typeof inputId !== 'string') return false;
	const authoritativeState = readMessageDeliveryState(data?.delivery_state);
	// A post-admission send error can beat the asynchronous cross-device row
	// broadcast. When it carries canonical delivery state, it is not evidence
	// that the user's input was rejected: settle local pending state from the
	// server truth so an equal-revision queued receipt cannot be mistaken for a
	// stale update later.
	const transition = authoritativeState === null
		? rejectInput(threadId, inputId)
		: recoverInput(threadId, inputId);
	if (transition.inputResolved && isDisplayedThread(threadId)) {
		const deliveryState = authoritativeState ?? 'failed';
		const deliveryError = !['queued', 'delivered'].includes(deliveryState)
			? (typeof data?.delivery_error === 'string' && data.delivery_error.trim()
				? data.delivery_error
				: typeof data?.message === 'string' && data.message.trim()
					? data.message
					: 'Message was not delivered')
			: null;
		const canonicalMessageId = typeof data?.message_id === 'string'
			&& data.message_id ? data.message_id : null;
		messages.update(current => current.map(message =>
			(message.metadata?.input_id === inputId || message.id === inputId)
				&& (
					message._optimistic === true
					|| message.delivery_state === 'queued'
					|| authoritativeState !== null
				)
				? mergeMessageDelivery(message, {
					...message,
					_optimistic: false,
					...(canonicalMessageId ? { id: canonicalMessageId } : {}),
					delivery_state: deliveryState,
					delivery_error: deliveryError,
					...(typeof data.delivery_revision === 'number'
						? { delivery_revision: data.delivery_revision }
						: {}),
				})
				: message
		));
	}
	return transition.inputResolved;
}

export function handleRecoveredInput(
	threadId: string,
	inputId: string,
	message: any,
): void {
	const transition = recoverInput(threadId, inputId);
	if (!transition.inputResolved) return;
	if (typeof message?.id === 'string' && typeof message?.created_at === 'string') {
		const deliveryState = readMessageDeliveryState(message.delivery_state);
		reconcileOptimisticRow(
			threadId, inputId, message.id, message.created_at,
			deliveryState !== null
				? {
					delivery_state: deliveryState,
					delivery_error: typeof message.delivery_error === 'string'
						? message.delivery_error
						: null,
					...(typeof message.delivery_revision === 'number'
						? { delivery_revision: message.delivery_revision }
						: {}),
				}
				: { delivery_state: 'delivered', delivery_error: null },
		);
	}
}

/**
 * Settle a thread the server has told us holds no live turn.
 *
 * No live turn on the server means nothing on screen belongs to one, so the
 * lifecycle settles and every transient the turn owned goes with it. The one
 * statement of that, because more than one server frame makes it: an
 * identity-less `session_snapshot`, and a Stop receipt that reports the turn
 * was already over.
 */
function settleThreadWithoutTurn(threadId: string): void {
	const prior = getTurnState(threadId);
	applySnapshot(threadId, null);
	if (prior.turnId !== null && prior.turnOrder !== null) {
		cancelThreadTypewriters(threadId, {
			turnId: prior.turnId,
			turnOrder: prior.turnOrder,
		});
	}
	clearThreadBuffers(threadId);
}

/**
 * Stop outcomes that are durable evidence the receipt's target is no longer live.
 *
 * `stopped` is the durable acknowledgement that the named turn ended; no later
 * terminal frame is required. `stale_active` says a DIFFERENT turn is running
 * and must not be touched, so it is excluded (USER-REPORTS #6a/#6b).
 */
const SETTLED_STOP_OUTCOMES: ReadonlySet<string> = new Set([
	'already_terminal',
	'no_active_turn',
	'stopped',
]);

/** Apply one Stop receipt: settle the thread when the receipt proves it ended. */
export function handleStopReceipt(data: any): void {
	const threadId = data?.thread_id;
	if (typeof threadId !== 'string' || !threadId) {
		emit('stop_receipt', data);
		return;
	}
	// A receipt naming a turn only settles that turn: a successor admitted
	// between the Stop and its receipt is a live turn this receipt says nothing
	// about. A receipt with no turn id is about the thread as a whole.
	const receiptTurnId = typeof data?.turn_id === 'string' ? data.turn_id : null;
	const settles = SETTLED_STOP_OUTCOMES.has(data?.outcome)
		&& (receiptTurnId === null || getTurnState(threadId).turnId === receiptTurnId);
	if (settles) settleThreadWithoutTurn(threadId);
	emit('stop_receipt', data);
}

export function handleThreadSnapshot(data: any) {
	const threadId = data?.thread_id;
	if (!threadId) return;
	const identity = frameIdentity(data);
	if (identity === null) {
		settleThreadWithoutTurn(threadId);
		emit('session_snapshot', data);
		return;
	}
	const transition = applySnapshot(threadId, identity, ...frameTurnFacts(data));
	if (transition.lifecycle === 'ignored' || transition.next.phase !== 'active') {
		const current = transition.next;
		if (
			current.phase === 'active'
			&& current.turnId !== null
			&& current.turnOrder !== null
		) {
			applyPendingForIdentity(threadId, {
				turnId: current.turnId,
				turnOrder: current.turnOrder,
			});
		}
		emit('session_snapshot', data);
		return;
	}
	resetPredecessor(threadId, transition);
	const state = data.state;
	const pending = takePendingDeltas(threadId, identity);
	if (state) {
		setThreadReasoning(threadId, state.reasoning || '');
		setThreadContent(threadId, state.content || '');
		setThreadActiveTools(threadId, (state.active_tools || []) as ActiveTool[]);
		markSeqApplied(threadId, identity, state.seq ?? 0);
	} else {
		clearThreadBuffers(threadId);
	}
	for (const delta of pending) applyDelta(delta, identity);
	emit('session_snapshot', data);
}

export function handleStepReasoning(data: any) {
	const identity = admitActiveFrame(data);
	if (!identity) return;
	const threadId = data.thread_id;
	if (isPendingSnapshot(threadId)) queuePending(threadId, data);
	else applyDelta(data, identity);
	emit('step_reasoning', data);
}

export function handleTextDelta(data: any) {
	const identity = admitActiveFrame(data);
	if (!identity) return;
	const threadId = data.thread_id;
	if (isPendingSnapshot(threadId)) queuePending(threadId, data);
	else applyDelta(data, identity);
	emit('text_delta', data);
}

export function upsertMessageByServerOrder(current: Message[], incoming: Message): {
	rows: Message[];
	inserted: boolean;
} {
	const existing = current.find(message => message.id === incoming.id);
	// A partial reveal survives a merge ONLY while its animation is still
	// running. Carrying the position across unconditionally is how a stale 0
	// outlived the reveal that set it and left the message invisible.
	const revealing = existing !== undefined
		&& isRevealing(incoming.thread_id, incoming.id);
	const merged: Message = existing
		? {
			...existing,
			...incoming,
			typewriterPos: revealing ? existing.typewriterPos : undefined,
		}
		: incoming;
	const rows = current.filter(message => message.id !== incoming.id);
	rows.push(merged);
	rows.sort(compareMessageOrder);
	return { rows, inserted: existing === undefined };
}

export function handleStepText(data: any) {
	if (
		typeof data?.message_id !== 'string'
		|| typeof data?.created_at !== 'string'
	) return;
	const identity = admitActiveFrame(data);
	if (!identity) return;
	const threadId = data.thread_id;
	const reasoningBuffer = get(liveReasoningByThread).get(threadId) || '';
	const hadStreamedContent = (get(streamContentByThread).get(threadId) || '').length > 0;
	let inserted = false;
	const message = stepTextMessage(data, threadId, reasoningBuffer);
	if (isDisplayedThread(threadId)) {
		messages.update(current => {
			const upserted = upsertMessageByServerOrder(current, message);
			inserted = upserted.inserted;
			return upserted.rows;
		});
		if (inserted && !hadStreamedContent) {
			startTypewriter(
				threadId, message.id, 0, data.content.length, identity,
			);
		}
	}
	const finalizedToolIds = Array.isArray(data.finalized_tool_ids)
		? data.finalized_tool_ids.filter((toolId: unknown): toolId is string =>
			typeof toolId === 'string'
		)
		: [];
	if (finalizedToolIds.length > 0) {
		const finalized = new Set(finalizedToolIds);
		updateThreadActiveTools(
			threadId,
			tools => tools.filter(tool => !finalized.has(tool.id)),
		);
	}
	commitLiveStep(threadId, data.content);
	const streamSeq = typeof data.stream_seq === 'number'
		? data.stream_seq
		: data.seq;
	if (typeof streamSeq === 'number') {
		markSeqApplied(threadId, identity, streamSeq);
	}
	emit('step_text', data);
}

export function handleToolStart(data: any) {
	if (!admitActiveFrame(data)) return;
	const threadId = data.thread_id;
	updateThreadActiveTools(threadId, tools => [...tools, startActiveTool(data)]);
	emit('tool_start', data);
}

export function handleToolDone(data: any) {
	if (!admitActiveFrame(data)) return;
	const threadId = data.thread_id;
	updateThreadActiveTools(threadId, tools =>
		tools.map(tool => tool.id === data.tool_id ? settleActiveTool(tool, data) : tool)
	);
}

export function handleTurnActivity(data: any) {
	if (admitActiveFrame(data)) emit('turn_activity', data);
}

export function handleContextUsageUpdated(data: any) {
	const threadId = data?.thread_id;
	if (typeof threadId !== 'string' || !isDisplayedThread(threadId)) return;
	if (!data.context_usage) return;
	if (frameIdentity(data) && !admitActiveFrame(data)) return;
	recordMeasuredUsage(threadId, data.context_usage);
	emit('context_usage_updated', data);
}

function clearTerminalTransients(threadId: string, identity: TurnIdentity): void {
	clearThreadBuffers(threadId);
	cancelThreadTypewriters(threadId, identity);
}

export function handleTurnInterrupted(data: any) {
	const threadId = data?.thread_id;
	const identity = admitTerminalFrame(data);
	if (!threadId || !identity) return;
	clearTerminalTransients(threadId, identity);
	emit('turn_interrupted', data);
}

export function handleTurnComplete(data: any) {
	const threadId = data?.thread_id;
	const identity = admitTerminalFrame(data);
	if (!threadId || !identity) return;
	clearTerminalTransients(threadId, identity);
	emit('turn_complete', data);
	if (!isDisplayedThread(threadId)) return;
	recordMeasuredUsage(threadId, data.context_usage ?? null);
	if (data.context_usage) {
		const deliveredContext = data.context_usage;
		const contextMessageId = data.context_message_id;
		messages.update(current => {
			if (typeof contextMessageId !== 'string' || !contextMessageId) return current;
			const targetIndex = current.findIndex(message =>
				message.thread_id === threadId && message.id === contextMessageId
			);
			if (targetIndex < 0) return current;
			const target = current[targetIndex];
			if (
				target.role !== 'assistant'
				&& !(target.role === 'system' && target.metadata?.type === 'result')
			) return current;
			return current.map((message, messageIndex) => {
				if (messageIndex !== targetIndex) return message;
				const nextMetadata: Record<string, unknown> = {
					...(message.metadata ?? {}),
					context_usage: deliveredContext,
				};
				return {
					...message,
					metadata: nextMetadata,
				};
			});
		});
	}
}

export function handleTurnCancelled(data: any) {
	const threadId = data?.thread_id;
	const identity = admitTerminalFrame(data);
	if (!threadId || !identity) return;
	clearTerminalTransients(threadId, identity);
	emit('turn_cancelled', data);
}

export function handleTurnError(data: any): boolean {
	const threadId = data?.thread_id;
	const identity = admitTerminalFrame(data);
	if (!threadId || !identity) return false;
	clearTerminalTransients(threadId, identity);
	return true;
}
