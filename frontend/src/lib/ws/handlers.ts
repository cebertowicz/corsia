import { get } from 'svelte/store';
import {
	messages,
	threads,
	upsertVisibleThread,
	mergeMessageDelivery,
} from '../stores/threads';
import type { Message } from '../stores/threads';
import { isDisplayedThread } from '../stores/displayedThreads';
import { readMessageDeliveryState } from '../messageDelivery';
import { emit } from './events';
import { registerSubagent, setSubagentStatus, setSubagentInit } from '../stores/subagents';
import { handleAuthOk, handleSystemDraining, handleWsError } from './handlersConnection';
import { compareMessageOrder } from '../messageHistory';
import {
	handleInputAccepted,
	handleContextUsageUpdated,
	handleThreadSnapshot,
	handleStepReasoning,
	handleStepText,
	handleStopReceipt,
	handleTextDelta,
	handleToolDone,
	handleToolStart,
	handleTurnActivity,
	handleTurnCancelled,
	handleTurnComplete,
	handleTurnInterrupted,
	upsertMessageByServerOrder,
} from './handlersTurns';
import { recoverInput } from './turnState';
import { acceptThreadModelTruth } from '../stores/models';
import { handleCheckpointChanged } from './checkpointInvalidation';
import { acceptsThreadAccountFrame, threadAccountFields } from '../stores/threadVendorAccounts';

import { threadSwitchFields } from './threadSwitchFields';
import { FORWARDED_TYPES } from './forwardedTypes';

function reconcileCorrelatedMessage(
	current: Message[],
	incoming: Message,
	inputId: string | null,
): Message[] {
	const correlated = inputId ? current.find(message =>
		message.metadata?.input_id === inputId
		|| (message._optimistic === true && message.id === inputId)
	) : undefined;
	const existing = correlated ?? current.find(message => message.id === incoming.id);
	const reconciled: Message = correlated
		? { ...mergeMessageDelivery(correlated, incoming), _optimistic: false }
		: existing
			? { ...mergeMessageDelivery(existing, incoming), typewriterPos: existing.typewriterPos }
			: incoming;
	return [
		...current.filter(message => message !== existing && message.id !== incoming.id),
		reconciled,
	];
}

function addedMessage(payload: any, threadId: string): Message | null {
	if (typeof payload?.id !== 'string' || typeof payload?.created_at !== 'string') return null;
	const revision = deliveryRevision(payload.delivery_revision);
	const deliveryState = readMessageDeliveryState(payload.delivery_state);
	const sequence = typeof payload.seq === 'number'
		&& Number.isInteger(payload.seq) && payload.seq > 0 ? payload.seq : undefined;
	return {
		id: payload.id,
		thread_id: threadId,
		role: payload.role,
		content: payload.content,
		metadata: payload.metadata,
		created_at: payload.created_at,
		...(sequence !== undefined ? { seq: sequence } : {}),
		...(payload.active_turn_id === null || typeof payload.active_turn_id === 'string'
			? { active_turn_id: payload.active_turn_id } : {}),
		...(payload.write_outcome === null || ['accepted', 'proved_no_write', 'ambiguous'].includes(payload.write_outcome)
			? { write_outcome: payload.write_outcome } : {}),
		...(deliveryState !== null
			? {
				delivery_state: deliveryState,
				delivery_error: typeof payload.delivery_error === 'string'
					? payload.delivery_error : null,
				...(revision !== undefined ? { delivery_revision: revision } : {}),
			}
			: {}),
	};
}

function deliveryRevision(value: unknown): number | undefined {
	return typeof value === 'number' && Number.isInteger(value) && value >= 1
		? value
		: undefined;
}

function reconcileQueuedInputReceipt(data: any): void {
	const threadId = typeof data?.thread_id === 'string' ? data.thread_id : null;
	const inputId = typeof data?.input_id === 'string' ? data.input_id : null;
	const messageId = typeof data?.message_id === 'string' ? data.message_id : null;
	if (!threadId || !inputId || !messageId || !isDisplayedThread(threadId)) return;
	const queuedAt = typeof data?.created_at === 'string' ? data.created_at : null;
	const revision = deliveryRevision(data?.delivery_revision);
	messages.update(current => {
		const correlated = current.find(message =>
			message.metadata?.input_id === inputId || message.id === inputId
		);
		if (!correlated) return current;
		const canonical = current.find(message => message.id === messageId);
		const receipt: Message = {
			...correlated,
			id: messageId,
			created_at: queuedAt ?? correlated.created_at,
			_optimistic: false,
			delivery_state: 'queued',
			delivery_error: null,
			...(revision !== undefined ? { delivery_revision: revision } : {}),
		};
		const reconciled = mergeMessageDelivery(canonical ?? correlated, receipt);
		const withoutDuplicates = current.filter(message =>
			message !== correlated && message.id !== messageId
		);
		return upsertMessageByServerOrder(withoutDuplicates, reconciled).rows;
	});
}

export function handleMessage(data: any, resetHeartbeat: () => void, isCurrentOwner = () => true) {
	if (FORWARDED_TYPES.has(data.type)) { emit(data.type, data); return; }
	switch (data.type) {
		case 'subagent_spawned': {
			if (data.child_thread_id) {
				registerSubagent(data.thread_id, data.child_thread_id, data.name || 'subagent', data.provider || 'cc', 'running');
			}
			emit('subagent_spawned', data);
			break;
		}
		case 'subagent_status':
			if (data.child_thread_id) setSubagentStatus(data.child_thread_id, data.status);
			break;

		case 'subagent_init':
			if (data.thread_id) setSubagentInit(data.thread_id, { model: data.model, cwd: data.cwd });
			break;

		case 'subagent_meta':
			if (data.thread_id) {
				registerSubagent(data.parent_thread_id, data.thread_id, data.name || 'subagent', data.provider || 'cc', data.live ? 'running' : 'idle');
				if (data.model) setSubagentInit(data.thread_id, { model: data.model });
			}
			break;

		case 'subagent_closed':
			if (data.thread_id) setSubagentStatus(data.thread_id, 'closed');
			emit('subagent_closed', data);
			break;

		case 'session_created':
			if (data.session) upsertVisibleThread(data.session);
			emit('session_created', data);
			break;

		case 'plans_changed':
			// Invalidation nudge only: consumers re-read through the HTTP reads
			// that own plan truth (plan pane, chat-list plan state, attention).
			// Without this case the nudge is dropped and nothing re-reads, so a
			// plan that just changed keeps showing its old state on the chat
			// list until the next reconnect.
			emit('plans_changed', data);
			break;

		case 'host_access_changed':
			// Routing hint only. The global control refreshes authoritative HTTP
			// state and never trusts lease fields carried by this frame.
			emit('host_access_changed', data);
			break;

		case 'ping':
			resetHeartbeat();
			break;

		case 'auth_ok':
			handleAuthOk(data, resetHeartbeat, isCurrentOwner);
			break;

		case 'system_draining':
			handleSystemDraining(data);
			break;

		case 'cache_warm_interrupted': {
			import('../stores/warmToast').then(({ showWarmToast }) => {
				showWarmToast(data.message || 'Cache still warming; first response may take longer.', 'rebuilding', 5000);
			});
			emit('cache_warm_interrupted', data);
			break;
		}

		case 'error':
			handleWsError(data);
			break;

		case 'input_accepted':
			handleInputAccepted(data);
			break;

		case 'input_queued': {
			const inputId = typeof data.input_id === 'string' ? data.input_id : null;
			if (inputId && typeof data.thread_id === 'string') {
				recoverInput(data.thread_id, inputId);
			}
			if (data.queue_kind === 'messages') reconcileQueuedInputReceipt(data);
			emit('input_queued', data);
			break;
		}
		case 'checkpoint_changed':
			handleCheckpointChanged(data.thread_id);
			break;

		case 'session_snapshot': {
			handleThreadSnapshot(data);
			break;
		}

		case 'step_reasoning': {
			handleStepReasoning(data);
			break;
		}

		case 'text_delta': {
			handleTextDelta(data);
			break;
		}

		case 'step_text': {
			handleStepText(data);
			break;
		}

		case 'tool_start': {
			handleToolStart(data);
			break;
		}

		case 'tool_done': {
			handleToolDone(data);
			break;
		}

		case 'turn_activity': {
			handleTurnActivity(data);
			break;
		}

		case 'context_usage_updated': {
			handleContextUsageUpdated(data);
			break;
		}

		case 'turn_interrupted': {
			handleTurnInterrupted(data);
			break;
		}

		case 'turn_complete': {
			handleTurnComplete(data);
			break;
		}

		case 'turn_cancelled': {
			handleTurnCancelled(data);
			break;
		}

		case 'stop_receipt': {
			handleStopReceipt(data);
			break;
		}

		case 'session_updated': {
			if (!acceptsThreadAccountFrame(data)) break;
			const carriesModelTruth = data.model !== undefined;
			if (carriesModelTruth) {
				if (
					typeof data.model !== 'string'
					|| !data.model
					|| typeof data.updated_at !== 'string'
					|| !data.updated_at
				) {
					console.warn('Ignoring session_updated without complete model truth');
					break;
				}
				if (!acceptThreadModelTruth(
					data.thread_id,
					data.model,
					data.updated_at,
				)) break;
			}
			threads.update(ss => {
				const updated = ss.map(s => {
					if (s.id !== data.thread_id) return s;
					return {
						...s,
						...(data.title !== undefined ? { title: data.title } : {}),
						...(data.last_message !== undefined ? { last_message: data.last_message } : {}),
						...threadSwitchFields(data),
						...threadAccountFields(data),
						...(!carriesModelTruth && data.updated_at !== undefined
							? { updated_at: data.updated_at }
							: {}),
					};
				});
				if (!carriesModelTruth && data.updated_at !== undefined) {
					updated.sort((a, b) => {
						const ta = a.updated_at ? new Date(a.updated_at.endsWith('Z') ? a.updated_at : a.updated_at + 'Z').getTime() : 0;
						const tb = b.updated_at ? new Date(b.updated_at.endsWith('Z') ? b.updated_at : b.updated_at + 'Z').getTime() : 0;
						return tb - ta;
					});
				}
				return updated;
			});
			break;
		}

		case 'models_updated': {
			import('../stores/models').then(m => m.loadModels());
			emit('models_updated', data);
			break;
		}

		case 'message_added': {
			const threadId = typeof data.thread_id === 'string' ? data.thread_id : null;
			if (!threadId) break;
			const injected = addedMessage(data.message, threadId);
			if (!injected) break;
			const inputId = typeof injected.metadata?.input_id === 'string'
				? injected.metadata.input_id : null;
			if (isDisplayedThread(threadId)) {
				messages.update(current =>
					reconcileCorrelatedMessage(current, injected, inputId).sort(compareMessageOrder)
				);
			}
			emit('message_added', data);
			break;
		}

		case 'messages_added': {
			const threadId = typeof data.thread_id === 'string' ? data.thread_id : null;
			if (!threadId || !Array.isArray(data.messages) || data.messages.length === 0) break;
			const batch = data.messages
				.map((payload: any) => addedMessage(payload, threadId))
				.filter((message: Message | null): message is Message => message !== null);
			if (batch.length !== data.messages.length) break;
			if (isDisplayedThread(threadId)) {
				messages.update(current => {
					let reconciled = current;
					for (const message of batch) {
						const inputId = typeof message.metadata?.input_id === 'string'
							? message.metadata.input_id : null;
						reconciled = reconcileCorrelatedMessage(reconciled, message, inputId);
					}
					return reconciled.sort(compareMessageOrder);
				});
			}
			emit('messages_added', data);
			break;
		}
	}
}
