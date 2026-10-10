import { messages } from '$lib/stores/threads';

export type ToolResultState = 'absent' | 'deferred' | 'loading' | 'failed' | 'loaded';

function updateToolCall(
	threadId: string,
	messageId: string,
	toolCallId: string,
	update: (call: any) => any,
): void {
	messages.update((current) => current.map((message) => {
		if (message.thread_id !== threadId || message.id !== messageId) return message;
		const calls = message.metadata?.tool_calls;
		if (!Array.isArray(calls)) return message;
		return {
			...message,
			metadata: {
				...message.metadata,
				tool_calls: calls.map((call: any) => call?.id === toolCallId ? update(call) : call),
			},
		};
	}));
}

/** Load one deferred tool result and persist its visible state in the message store. */
export async function loadDeferredToolResult(
	threadId: string,
	messageId: string,
	toolCallId: string,
	authToken: string,
): Promise<void> {
	updateToolCall(threadId, messageId, toolCallId, (call) => ({
		...call,
		result_state: 'loading' satisfies ToolResultState,
		result_error: undefined,
	}));
	try {
		const response = await fetch(
			`/api/threads/${encodeURIComponent(threadId)}/messages/${encodeURIComponent(messageId)}/tool-results/${encodeURIComponent(toolCallId)}?token=${encodeURIComponent(authToken)}`,
		);
		const data = response.ok ? await response.json() : null;
		if (typeof data?.result !== 'string') throw new Error('Tool result unavailable');
		updateToolCall(threadId, messageId, toolCallId, (call) => ({
			...call,
			result: data.result,
			result_state: 'loaded' satisfies ToolResultState,
			result_error: undefined,
		}));
	} catch {
		updateToolCall(threadId, messageId, toolCallId, (call) => ({
			...call,
			result_state: 'failed' satisfies ToolResultState,
			result_error: 'Could not load output.',
		}));
	}
}
