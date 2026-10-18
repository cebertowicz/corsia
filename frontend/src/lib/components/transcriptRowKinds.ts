import type { TranscriptMessage } from './messageListModel';
import type { TranscriptFoldEntry } from './transcriptFocusModel';

export function isHumanMessage(message: TranscriptMessage): boolean {
	return message.role === 'user' && message.metadata?.sender_kind !== 'agent';
}

// System rows that only record activity. Every other system row is a notice
// (an error, interrupt, sandbox denial, or a type added later) and stays
// visible, so Focus never buries something the user must act on. A `seed` is a
// session's starting context, which the transcript never draws.
const ACTIVITY_SYSTEM_TYPES = new Set([
	'context_event', 'activity_receipt', 'subagent_ref', 'subagent_done', 'send_ref',
	'artifact_removed', 'model_switch', 'account_switch', 'seed',
]);

// Rows that end a turn with an error. One stays a notice until the agent's next turn
// starts with nothing to read in between; that start writes `recovered: true` on it.
const ERROR_SYSTEM_TYPES = new Set([
	'llm_error', 'cc_error', 'tool_boundary_violation', 'tool_bridge_unavailable',
]);

export function systemType(message: TranscriptMessage): string {
	return String(message.metadata?.type ?? '');
}

/** Whether an error row carries the stamp that moves it inside its block. */
export function isRecoveredError(message: TranscriptMessage): boolean {
	return message.role === 'system' && ERROR_SYSTEM_TYPES.has(systemType(message))
		&& message.metadata?.recovered === true;
}

export function isSystemNotice(message: TranscriptMessage): boolean {
	if (message.role !== 'system') return false;
	const type = systemType(message);
	if (ERROR_SYSTEM_TYPES.has(type)) return message.metadata?.recovered !== true;
	// A provider result splits only as an error with something to read.
	if (type === 'result') return message.metadata?.is_error === true && Boolean(message.content);
	return !ACTIVITY_SYSTEM_TYPES.has(type);
}

/** The kinds a run stub names; the vocabulary is shared with the backend's `row_kind`. */
export type RowKind =
	| 'steps' | 'context_update' | 'account_move' | 'model_switch'
	| 'subagent_finished' | 'agent_message' | 'error_recovered' | 'other';

/**
 * Whether Focus shows `message` as its own item, which closes the open block.
 * The client twin of the backend's `is_focus_visible`: one JSON fixture runs both.
 */
export function isFocusVisible(message: TranscriptMessage): boolean {
	if (message.role === 'user') return isHumanMessage(message);
	if (message.role === 'assistant') return message.metadata?.for_user === true && Boolean(message.content);
	return isSystemNotice(message);
}

/**
 * The kind a row gives its block's stub. The twin of the backend's `row_kind`: an
 * assistant row is `steps` once its server weight is above zero; a row Focus shows on
 * its own has no kind of its own and reads `other`.
 */
export function rowKind(message: TranscriptMessage): RowKind {
	if (message.role === 'user') return message.metadata?.sender_kind === 'agent' ? 'agent_message' : 'other';
	if (message.role === 'assistant') {
		const weight = message.activity_weight;
		// A live frame has no weight yet; its activity is what makes it a step.
		return (typeof weight === 'number' ? weight > 0 : hasAssistantActivity(message)) ? 'steps' : 'other';
	}
	if (message.role !== 'system') return 'other';
	const type = systemType(message);
	if (ERROR_SYSTEM_TYPES.has(type)) return isRecoveredError(message) ? 'error_recovered' : 'other';
	if (type === 'context_event') return 'context_update';
	if (type === 'account_switch') return 'account_move';
	if (type === 'subagent_done') return 'subagent_finished';
	const modelReceipt = type === 'activity_receipt'
		&& (message.metadata?.receipt_type === 'model_switch' || message.metadata?.event_type === 'model.switched');
	return type === 'model_switch' || modelReceipt ? 'model_switch' : 'other';
}

/** The kinds a block holds: its stub's, with the rows loaded now, sorted. */
export function runKinds(run: Pick<TranscriptFoldEntry, 'stub' | 'items'>): RowKind[] {
	const kinds = new Set<string>(run.stub?.kinds ?? []);
	for (const item of run.items) kinds.add(rowKind(item.message));
	return [...kinds].sort() as RowKind[];
}

export function hasAssistantActivity(message: TranscriptMessage): boolean {
	return Boolean(
		message.reasoning
		|| message.toolHints?.length
		|| message.metadata?.tool_calls?.length,
	);
}
