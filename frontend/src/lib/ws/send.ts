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
} from './turnState';
import { beginPendingSnapshot } from './streamState';
import { takeReplyContext } from '../stores/replyContextCapture';

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

export function sendMessage(
	threadId: string,
	content: string,
	modelId?: string,
	attachments?: any[],
	extraMeta?: Record<string, any>,
	deliveryIntent: 'apply_now' | 'queue_after_turn' = 'apply_now',
): string | null {
	if (!ws || ws.readyState !== WebSocket.OPEN) return null;
	const inputId = crypto.randomUUID();
	const viewingDocument = viewingDocumentForThread(threadId);
	const viewingPayload = viewingDocument ? { viewing_document: viewingDocument } : {};

	const metaObj: Record<string, any> = { input_id: inputId };
	if (attachments && attachments.length > 0) metaObj.attachments = attachments;
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
	// ordinary queued row that the transcript renders like any other, so there
	// is nothing to track in a side store and nothing to reconcile later.
	requestInput(threadId, inputId);

	if (get(subagentMeta).has(threadId)) {
		ws.send(JSON.stringify({
			type: 'subagent_feed', thread_id: threadId, content, input_id: inputId,
			...viewingPayload,
			...(attachments && attachments.length > 0 ? { attachments } : {}),
			...(deliveryIntent === 'queue_after_turn' ? { delivery_intent: deliveryIntent } : {}),
		}));
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
	// What the person was answering, taken at send so the window it accumulated in ends with it.
	const replyContext = takeReplyContext(threadId);
	ws.send(JSON.stringify({
		type: 'message',
		...viewingPayload,
		...(replyContext ? { reply_context: replyContext } : {}),
		thread_id: threadId,
		content,
		input_id: inputId,
		...(modelId ? { model_id: modelId } : {}),
		...(effort ? { reasoning_effort: effort } : {}),
		...(attachments && attachments.length > 0 ? { attachments } : {}),
		...(deliveryIntent === 'queue_after_turn' ? { delivery_intent: deliveryIntent } : {}),
	}));
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
