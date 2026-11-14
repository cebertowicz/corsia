import type { Message } from '$lib/stores/threads';
import { isNeverDrawn } from '$lib/components/transcriptRowKinds';

/** Show each answering step's chat text without its placed answers, and drop a step the server stamped as drawing nothing. */
export function visibleChatAnswerRows(rows: Message[]): Message[] {
	return rows.flatMap((row) => {
		if (row.role !== 'assistant') return [row];
		if (isNeverDrawn(row)) return [];
		return typeof row.chat_visible_content === 'string' ? [{ ...row, content: row.chat_visible_content }] : [row];
	});
}
