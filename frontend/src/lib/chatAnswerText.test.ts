import { describe, expect, it } from 'vitest';
import type { Message } from './stores/threads';
import { visibleChatAnswerRows } from './chatAnswerText';
import { buildMessageListPresentation, buildTranscriptPresentation } from './components/messageListModel';

const message = (values: Partial<Message>): Message => ({
	id: 'a', thread_id: 'thread', role: 'assistant', content: '[c1.1] answer', created_at: 't', ...values,
});

describe('placed chat answer rows', () => {
	it('shows the server-provided chat text, without the placed answers', () => {
		expect(visibleChatAnswerRows([message({ chat_visible_content: 'Introduction' })])[0].content).toBe('Introduction');
	});
	it('drops a step the server stamped as drawing nothing, and never works that out from the row', () => {
		expect(visibleChatAnswerRows([message({ chat_visible_content: '', metadata: { chat_drawn: false } })])).toEqual([]);
		// An empty chat text alone does not hide a row: the stamp is the one answer.
		const unstamped = message({ chat_visible_content: '' });
		expect(visibleChatAnswerRows([unstamped])).toEqual([{ ...unstamped, content: '' }]);
	});
	it('retains a row with other content and leaves unprojected messages alone', () => {
		const row = message({ chat_visible_content: '', reasoning: 'work' });
		expect(visibleChatAnswerRows([row])[0]).toMatchObject({ content: '', reasoning: 'work' });
		const unprojected = message({});
		expect(visibleChatAnswerRows([unprojected])[0]).toBe(unprojected);
	});
});

describe('a run made only of stamped steps', () => {
	const stamped = (id: string) => message({ id, seq: 2, chat_visible_content: '', metadata: { turn_id: 'turn-1', chat_drawn: false } });
	const present = (rows: Message[], live: boolean) => buildTranscriptPresentation(
		buildMessageListPresentation(visibleChatAnswerRows(rows)).visibleMessages, true, live ? { turnId: 'turn-1' } : null,
	);
	it('shows one working line while the turn is live', () => {
		const entries = present([stamped('a')], true);
		expect(entries).toHaveLength(1);
		expect(entries[0]).toMatchObject({ kind: 'fold', live: true, items: [] });
	});
	it('leaves no folded line once the turn ends', () => {
		expect(present([stamped('a')], false)).toEqual([]);
	});
	it('folds a mixed turn by counting only the steps it draws', () => {
		const drawn = message({ id: 'b', seq: 3, content: 'Working', metadata: { turn_id: 'turn-1' }, activity_weight: 1 } as Partial<Message>);
		const [entry] = present([stamped('a'), drawn], false);
		expect(entry).toMatchObject({ kind: 'fold', items: [{ message: { id: 'b' } }] });
		expect((entry as { items: unknown[] }).items).toHaveLength(1);
	});
});
