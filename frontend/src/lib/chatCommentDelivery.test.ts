import { describe, expect, it } from 'vitest';
import type { ChatComment } from '$lib/stores/threads';
import { failedSends } from './chatCommentDelivery';

const answer = (sender: string, state: string | null, author_kind: 'user' | 'agent' = 'user') => ({
	author_kind, user_message_id: sender, cid: `${sender}-cid`, text: `${sender} text`, created_at: '2026-11-12T00:00:01Z', unread: false,
	...(author_kind === 'agent' ? { assistant_message_id: 'assistant' } : { delivery_state: state }),
}) as ChatComment['answers'][number];
const comment = (state: string | null, answers: ChatComment['answers'] = []) => ({
	id: 'root', cid: 'c1', user_message_id: 'author', quote: 'text', note: 'my comment', from_off: 0, to_off: 4,
	anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-12T00:00:00Z', delivery_state: state, answers,
}) as ChatComment;

// The server puts each input's delivery on the comment it carried and on each reply the person wrote, so the
// thread reads its failures there; there is no copy of the input on this device.
describe('failed chat comment sends', () => {
	it('gives a comment whose own input failed its failure in its own thread', () => {
		expect(failedSends(comment('failed'))).toEqual([{ messageId: 'author', note: 'my comment', createdAt: '2026-11-12T00:00:00Z', root: true }]);
		for (const state of ['queued', 'delivered', 'cancelled', null]) expect(failedSends(comment(state))).toEqual([]);
	});

	it('lists each failed reply the person wrote, and no delivered reply or agent answer', () => {
		const answers = [answer('sent', 'delivered'), answer('broken', 'failed'), answer('waiting', 'queued'), answer('agent', null, 'agent')];
		expect(failedSends(comment('delivered', answers))).toEqual([{ messageId: 'broken', note: 'broken text', createdAt: '2026-11-12T00:00:01Z', root: false }]);
	});
});
