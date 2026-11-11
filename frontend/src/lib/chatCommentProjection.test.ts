import { describe, expect, it } from 'vitest';
import type { Message } from '$lib/stores/threads';
import { suppressibleCommentEnvelopes } from './chatCommentDelivery';

const comment = (id: string, state: string, content = '', attachments: unknown[] = []) => ({
	id, thread_id: 'thread', role: 'user', content, delivery_state: state,
	metadata: { reply_context: { comments: [{ id: 'one', note: 'note' }] }, attachments },
}) as Message;

describe('chat comment transcript projection', () => {
	it.each(['queued', 'delivered', 'failed', 'ambiguous', 'cancelled'])('hides %s comment-only inputs without the target loaded', state => {
		expect(suppressibleCommentEnvelopes([comment('a', state)], 'thread').has('a')).toBe(true);
	});
	it('keeps typed text and attachments visible', () => {
		const rows = [comment('text', 'delivered', 'also do this'), comment('file', 'delivered', '', [{}])];
		expect([...suppressibleCommentEnvelopes(rows, 'thread')]).toEqual([]);
	});
});
