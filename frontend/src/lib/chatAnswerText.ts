import type { Message } from '$lib/stores/threads';

/** Remove answer-only transcript rows while retaining other message content. */
export function visibleChatAnswerRows(rows: Message[]): Message[] {
	return rows.flatMap((row) => {
		if (row.role !== 'assistant' || typeof row.chat_visible_content !== 'string') return [row];
		const content = row.chat_visible_content;
		if (!content && !row.reasoning && !row.toolHints?.length && !row.metadata?.tool_calls?.length && !(row as any).chat_comments?.length && !row.cards?.length) return [];
		return [{ ...row, content }];
	});
}
