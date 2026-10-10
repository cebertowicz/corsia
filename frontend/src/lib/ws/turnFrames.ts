import { parseWsTimestamp } from '../time';
import type { Message } from '../stores/threads';
import type { TurnIdentity } from './turnState';

export function frameIdentity(data: any): TurnIdentity | null {
	return typeof data?.turn_id === 'string' && Number.isInteger(data?.turn_order)
		? { turnId: data.turn_id, turnOrder: data.turn_order }
		: null;
}

/**
 * The visible root the server attached to a Turn frame. Undefined when the
 * frame does not carry the field (its lineage stays untouched); a definite
 * null means the server could not resolve one.
 */
export function frameVisibleRootThreadId(data: any): string | null | undefined {
	if (data === null || typeof data !== 'object' || !('visible_root_thread_id' in data)) {
		return undefined;
	}
	return typeof data.visible_root_thread_id === 'string' ? data.visible_root_thread_id : null;
}

/**
 * The durable admission time a Turn frame carries, in epoch milliseconds, or
 * null when the frame names none (or one that does not parse).
 */
export function frameStartedAt(data: any): number | null {
	const startedAt = parseWsTimestamp(data?.started_at);
	return Number.isFinite(startedAt) ? startedAt : null;
}

/** What a Turn frame says about its Turn beyond identity: lineage, then start. */
export function frameTurnFacts(data: any): [string | null | undefined, number | null] {
	return [frameVisibleRootThreadId(data), frameStartedAt(data)];
}

/** The persisted assistant row a ``step_text`` frame describes. */
export function stepTextMessage(data: any, threadId: string, reasoningBuffer: string): Message {
	const reasoning = data.metadata?.reasoning || reasoningBuffer || undefined;
	return {
		id: data.message_id,
		thread_id: threadId,
		role: 'assistant',
		content: data.content,
		toolHints: data.metadata?.tool_hints?.length > 0 ? data.metadata.tool_hints : undefined,
		metadata: data.metadata,
		created_at: data.created_at,
		seq: typeof data.seq === 'number' ? data.seq : undefined,
		typewriterPos: undefined,
		reasoning,
	};
}
