import { describe, expect, it } from 'vitest';
import type { Message } from './stores/threads';
import { visibleChatAnswerRows } from './chatAnswerText';

const message = (values: Partial<Message>): Message => ({
	id: 'a', thread_id: 'thread', role: 'assistant', content: '[c1.1] answer', created_at: 't', ...values,
});

describe('placed chat answer rows', () => {
	it('uses server-provided visible content and hides an answer-only row', () => {
		expect(visibleChatAnswerRows([message({ chat_visible_content: '' })])).toEqual([]);
		expect(visibleChatAnswerRows([message({ chat_visible_content: 'Introduction' })])[0].content).toBe('Introduction');
	});
	it('retains a row with other content and leaves unprojected messages alone', () => {
		const row = message({ chat_visible_content: '', reasoning: 'work' });
		expect(visibleChatAnswerRows([row])[0]).toMatchObject({ content: '', reasoning: 'work' });
		const unprojected = message({});
		expect(visibleChatAnswerRows([unprojected])[0]).toBe(unprojected);
	});
});
