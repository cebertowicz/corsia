import { get, writable } from 'svelte/store';
import {
	messages, threads, reasoningEffort,
} from '../stores/threads';
import type { Message } from '../stores/threads';
import { isDisplayedThread } from '../stores/displayedThreads';
import { ws, subscribedThreads } from './state';
import { subagentMeta } from '../stores/subagents';
import { viewingDocumentForThread } from '../stores/documentViewport';
import {
	acknowledgedTurnId,
	hasActiveTurn as reducerHasActiveTurn,
	requestInput,
	turnStates,
} from './turnState';
import { beginPendingSnapshot } from './streamState';
import { takeReplyContext, type ReplyComment } from '../stores/replyContextCapture';

export function hasActiveTurn(threadId: string): boolean {
	return reducerHasActiveTurn(threadId);
}

/**
 * Views that follow a thread's live Turn without showing its transcript (a
 * plan row's live step), counted per thread. The server keeps one
 * subscription per socket, so the socket stays subscribed while either a
 * transcript view or any watcher still wants the thread.
 */
const threadWatchers = new Map<string, number>();

function sendSubscribe(threadId: string): void {
	if (!ws || ws.readyState !== WebSocket.OPEN) return;
	beginPendingSnapshot(threadId);
	ws.send(JSON.stringify({ type: 'subscribe_session', thread_id: threadId }));
}

function sendUnsubscribe(threadId: string): void {
	if (subscribedThreads.has(threadId) || threadWatchers.has(threadId)) return;
	if (ws && ws.readyState === WebSocket.OPEN) {
		ws.send(JSON.stringify({ type: 'unsubscribe_session', thread_id: threadId }));
	}
}

export function subscribeThread(threadId: string) {
	if (!ws || ws.readyState !== WebSocket.OPEN || !threadId) return;
	if (subscribedThreads.has(threadId)) return;
	subscribedThreads.add(threadId);
	sendSubscribe(threadId);
}

export function unsubscribeThread(threadId: string) {
	if (!threadId || !subscribedThreads.has(threadId)) return;
	subscribedThreads.delete(threadId);
	sendUnsubscribe(threadId);
}

/** Follow a thread's live Turn frames; returns the release. */
export function watchThread(threadId: string): () => void {
	const count = threadWatchers.get(threadId) ?? 0;
	threadWatchers.set(threadId, count + 1);
	if (count === 0 && !subscribedThreads.has(threadId)) sendSubscribe(threadId);
	let released = false;
	return () => {
		if (released) return;
		released = true;
		const remaining = (threadWatchers.get(threadId) ?? 1) - 1;
		if (remaining > 0) {
			threadWatchers.set(threadId, remaining);
			return;
		}
		threadWatchers.delete(threadId);
		sendUnsubscribe(threadId);
	};
}

/** A new socket starts with no subscriptions: resubscribe every watched thread. */
export function resubscribeWatchedThreads(): void {
	for (const threadId of threadWatchers.keys()) {
		if (!subscribedThreads.has(threadId)) sendSubscribe(threadId);
	}
}

/**
 * The exact frame each pending input put on the socket, keyed by input id.
 *
 * `ws.send` is fire-and-forget and can be swallowed whole: a half-dead socket
 * accepts the frame, the person sees their message, and the server never hears
 * of it, so the only copy of a lost frame is the one this client still holds.
 * Turn state owns membership: the subscription below drops a frame as soon as
 * its input stops being pending, a deleted thread's included, so the store can
 * never grow past the inputs actually waiting.
 */
const unconfirmedFrames = new Map<string, { threadId: string; frame: string }>();

turnStates.subscribe(states => {
	for (const [inputId, stored] of unconfirmedFrames) {
		const state = states.get(stored.threadId);
		if (!state?.pendingInputIds.includes(inputId)) unconfirmedFrames.delete(inputId);
	}
});

/**
 * Put every frame still waiting for a receipt on a new socket, in the order the
 * person sent them, before anything else can be sent on it.
 *
 * Each goes out marked `recovery`: the server answers one it already stores
 * with that stored row instead of admitting it again, and admits one it never
 * got. It admits a socket's messages one at a time in the order they arrive,
 * so a lost message is admitted before any message sent after the reconnect.
 */
export function resendUnconfirmedFrames(socket: WebSocket): void {
	for (const { frame } of unconfirmedFrames.values()) {
		socket.send(JSON.stringify({ ...JSON.parse(frame), recovery: true }));
	}
}

/** Which inputs this client still holds a frame for. Test-only visibility. */
export function _unconfirmedFrameIdsForTests(): string[] {
	return [...unconfirmedFrames.keys()];
}

export function sendMessage(
	threadId: string,
	content: string,
	modelId?: string,
	attachments?: any[],
	extraMeta?: Record<string, any>,
	deliveryIntent: 'apply_now' | 'queue_after_turn' = 'apply_now',
	comments: ReplyComment[] = [],
): string | null {
	if (!ws || ws.readyState !== WebSocket.OPEN) return null;
	const inputId = crypto.randomUUID();
	const viewingDocument = viewingDocumentForThread(threadId);
	const viewingPayload = viewingDocument ? { viewing_document: viewingDocument } : {};

	const metaObj: Record<string, any> = { input_id: inputId };
	if (attachments && attachments.length > 0) metaObj.attachments = attachments;
	// The sending socket gets no message_added for its own input, so the echo shows what was sent.
	if (comments.length) metaObj.reply_context = { comments };
	if (extraMeta) {
		const { delivery_intent: _ignoredDeliveryIntent, ...safeExtraMeta } = extraMeta;
		Object.assign(metaObj, safeExtraMeta);
	}
	if (deliveryIntent === 'queue_after_turn') {
		metaObj.delivery_intent = deliveryIntent;
	}

	const msg: Message = {
		id: inputId,
		thread_id: threadId,
		role: 'user',
		content,
		metadata: Object.keys(metaObj).length > 0 ? metaObj : undefined,
		created_at: new Date().toISOString(),
		_optimistic: true,
	};
	if (isDisplayedThread(threadId)) {
		messages.update(m => [...m, msg]);
	}
	// One queue. A message sent while a checkpoint holds the thread is an
	// ordinary queued row that the transcript renders like any other; the frame
	// it went out with is kept beside the pending row for as long as it waits.
	requestInput(threadId, inputId);

	if (get(subagentMeta).has(threadId)) {
		const frame = JSON.stringify({
			type: 'subagent_feed', thread_id: threadId, content, input_id: inputId,
			...viewingPayload,
			...(comments.length ? { reply_context: { comments } } : {}),
			...(attachments && attachments.length > 0 ? { attachments } : {}),
			...(deliveryIntent === 'queue_after_turn' ? { delivery_intent: deliveryIntent } : {}),
		});
		ws.send(frame);
		unconfirmedFrames.set(inputId, { threadId, frame });
		return inputId;
	}

	const now = new Date().toISOString();
	threads.update(ss => {
		const updated = ss.map(s =>
			s.id === threadId
				? { ...s, updated_at: now, last_message: content.slice(0, 100) }
				: s
		);
		updated.sort((a, b) => {
			const ta = a.updated_at ? new Date(a.updated_at.endsWith('Z') ? a.updated_at : a.updated_at + 'Z').getTime() : 0;
			const tb = b.updated_at ? new Date(b.updated_at.endsWith('Z') ? b.updated_at : b.updated_at + 'Z').getTime() : 0;
			return tb - ta;
		});
		return updated;
	});

	const effort = get(reasoningEffort);
	// The captured context belongs to this send.
	const replyContext = takeReplyContext(threadId) ?? {};
	if (comments.length) replyContext.comments = comments;
	const hasReplyContext = Object.keys(replyContext).length > 0;
	const frame = JSON.stringify({
		type: 'message',
		...viewingPayload,
		...(hasReplyContext ? { reply_context: replyContext } : {}),
		thread_id: threadId,
		content,
		input_id: inputId,
		...(modelId ? { model_id: modelId } : {}),
		...(effort ? { reasoning_effort: effort } : {}),
		...(attachments && attachments.length > 0 ? { attachments } : {}),
		...(deliveryIntent === 'queue_after_turn' ? { delivery_intent: deliveryIntent } : {}),
	});
	ws.send(frame);
	unconfirmedFrames.set(inputId, { threadId, frame });
	return inputId;
}

/**
 * The exact turn, per thread, this client has asked to stop.
 *
 * Stop used to be indistinguishable from broken: the request left no trace, so
 * a UI that kept offering Stop looked the same whether the turn had ignored it
 * or had already ended. Keyed by turn id, so the acknowledgement dies with the
 * turn and cannot bleed onto the next one.
 */
export const stopRequestedTurns = writable<ReadonlyMap<string, string>>(new Map());

function recordStopRequest(threadId: string, turnId: string | null): void {
	if (!turnId) return;
	stopRequestedTurns.update(current => {
		if (current.get(threadId) === turnId) return current;
		const next = new Map(current);
		next.set(threadId, turnId);
		return next;
	});
}

export function stopTurn(threadId: string) {
	if (!ws || ws.readyState !== WebSocket.OPEN) return;
	const turnId = acknowledgedTurnId(threadId);
	recordStopRequest(threadId, turnId);
	if (turnId && get(subagentMeta).has(threadId)) {
		ws.send(JSON.stringify({
			type: 'subagent_stop', thread_id: threadId, turn_id: turnId,
		}));
		return;
	}
	ws.send(JSON.stringify({
		type: 'stop', thread_id: threadId,
		...(turnId ? { turn_id: turnId } : {}),
	}));
}
