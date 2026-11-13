import { describe, expect, it } from 'vitest';
import type { ChatComment } from '$lib/stores/threads';
import { endedSends, needsAttention } from './chatCommentDelivery';

const answer = (sender: string, state: string | null, author_kind: 'user' | 'agent' = 'user') => ({
	author_kind, user_message_id: sender, cid: `${sender}-cid`, text: `${sender} text`, created_at: '2026-11-12T00:00:01Z', unread: false,
	...(author_kind === 'agent' ? { assistant_message_id: 'assistant' } : { delivery_state: state, delivery_error: null }),
}) as ChatComment['answers'][number];
const comment = (state: string | null, answers: ChatComment['answers'] = [], error: string | null = null) => ({
	id: 'root', cid: 'c1', user_message_id: 'author', quote: 'text', note: 'my comment', from_off: 0, to_off: 4,
	anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-12T00:00:00Z', delivery_state: state, delivery_error: error, answers,
}) as ChatComment;
const shown = (sends: ReturnType<typeof endedSends>) => sends.map(send => [send.messageId, send.delivery.label, send.delivery.failed, send.retry?.label ?? null]);

// The server puts each input's delivery on the comment it carried and on each reply the person wrote, so the
// thread reads how each ended there, and shows it as chat shows a message in that state.
describe('ended chat comment sends', () => {
	it('shows a comment whose own input ended with chat’s label, red mark and Retry for that state', () => {
		expect(shown(endedSends(comment('ambiguous')))).toEqual([['author', 'Delivery uncertain', true, 'Retry anyway']]);
		expect(shown(endedSends(comment('cancelled')))).toEqual([['author', 'Cancelled by you', false, null]]);
		const [failed] = endedSends(comment('failed'));
		expect([failed.delivery.label.startsWith('Not delivered: '), failed.delivery.failed, failed.retry?.label, failed.root]).toEqual([true, true, 'Retry', true]);
		for (const state of ['queued', 'delivered', null]) expect(endedSends(comment(state))).toEqual([]);
	});

	it('lists each ended reply the person wrote, and no delivered reply or agent answer', () => {
		const answers = [answer('sent', 'delivered'), answer('broken', 'failed'), answer('stopped', 'cancelled'), answer('waiting', 'queued'), answer('agent', null, 'agent')];
		expect(endedSends(comment('delivered', answers)).map(send => [send.messageId, send.note, send.root])).toEqual([['broken', 'broken text', false], ['stopped', 'stopped text', false]]);
	});

	it('names a comment or a reply in its Retry title', () => {
		const titles = (state: string) => endedSends(comment(state, [answer('reply', state)])).map(send => send.retry?.title);
		expect(titles('failed')).toEqual(['Send this comment again', 'Send this reply again']);
		expect(titles('ambiguous')).toEqual([
			'This may send the comment twice if the provider received it before the connection failed',
			'This may send the reply twice if the provider received it before the connection failed',
		]);
	});

	it('marks the comment for attention only where chat would put its red mark', () => {
		expect(needsAttention(comment('cancelled'))).toBe(false);
		expect(needsAttention(comment('delivered', [answer('broken', 'ambiguous')]))).toBe(true);
	});
});
