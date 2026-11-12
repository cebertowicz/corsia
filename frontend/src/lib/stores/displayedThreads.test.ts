import { get } from 'svelte/store';
import { beforeEach, describe, expect, it } from 'vitest';
import { activeThreadId, messages, type Message } from './threads';
import { markCommentOnlyTurn } from './displayedThreads';

function row(id: string, fields: Partial<Message>): Message {
	return { id, thread_id: 't1', role: 'assistant', content: '', created_at: '2026-11-12T00:00:00Z', ...fields };
}

describe('markCommentOnlyTurn', () => {
	beforeEach(() => {
		activeThreadId.set('t1');
		messages.set([
			row('digest', { role: 'system', active_turn_id: 'comment', metadata: { type: 'context_event', event_disposition: 'passive' } }),
			row('answer', { metadata: { turn_id: 'comment' } }),
			row('other-digest', { role: 'system', active_turn_id: 'ordinary', metadata: { type: 'context_event', event_disposition: 'passive' } }),
			row('other-work', { metadata: { turn_id: 'ordinary' } }),
		]);
	});

	it('marks the context digest the comment-only Turn consumed, not only rows naming the Turn', () => {
		markCommentOnlyTurn('t1', 'comment');
		const marked = get(messages).filter((message) => message.commentOnlyReceipt).map((message) => message.id);
		expect(marked).toEqual(['digest', 'answer']);
	});
});
