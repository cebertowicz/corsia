import { describe, expect, it, vi } from 'vitest';
import type { ChatComment } from '$lib/stores/threads';
import { unsentSends, needsAttention, retryRefusedForGood } from './chatCommentDelivery';

const answer = (sender: string, state: string | null, author_kind: 'user' | 'agent' = 'user') => ({
	author_kind, user_message_id: sender, cid: `${sender}-cid`, text: `${sender} text`, created_at: '2026-11-12T00:00:01Z', unread: false,
	...(author_kind === 'agent' ? { assistant_message_id: 'assistant' } : { delivery_state: state, delivery_error: null }),
}) as ChatComment['answers'][number];
const comment = (state: string | null, answers: ChatComment['answers'] = [], error: string | null = null) => ({
	id: 'root', cid: 'c1', user_message_id: 'author', quote: 'text', note: 'my comment', from_off: 0, to_off: 4,
	anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-12T00:00:00Z', delivery_state: state, delivery_error: error, answers,
}) as ChatComment;
const shown = (sends: ReturnType<typeof unsentSends>) => sends.map(send => [send.messageId, send.delivery.label, send.delivery.failed, send.retry?.label ?? null]);

// The server puts each input's delivery on the comment it carried and on each reply the person wrote, so the
// thread reads how each ended there, and shows it as chat shows a message in that state.
describe('ended chat comment sends', () => {
	it('shows a comment whose own input ended with chat’s label, red mark and Retry for that state', () => {
		expect(shown(unsentSends(comment('ambiguous')))).toEqual([['author', 'Delivery uncertain', true, 'Retry anyway']]);
		expect(shown(unsentSends(comment('cancelled')))).toEqual([['author', 'Cancelled by you', false, null]]);
		const [failed] = unsentSends(comment('failed'));
		expect([failed.delivery.label.startsWith('Not delivered: '), failed.delivery.failed, failed.retry?.label, failed.root]).toEqual([true, true, 'Retry', true]);
		for (const state of ['queued', 'delivered', null]) expect(unsentSends(comment(state))).toEqual([]);
	});

	it('lists each ended reply the person wrote, and no delivered reply or agent answer', () => {
		const answers = [answer('sent', 'delivered'), answer('broken', 'failed'), answer('stopped', 'cancelled'), answer('waiting', 'queued'), answer('agent', null, 'agent')];
		expect(unsentSends(comment('delivered', answers)).map(send => [send.messageId, send.note, send.root])).toEqual([['broken', 'broken text', false], ['stopped', 'stopped text', false]]);
	});

	it('names a comment or a reply in its Retry title', () => {
		const titles = (state: string) => unsentSends(comment(state, [answer('reply', state)])).map(send => send.retry?.title);
		expect(titles('failed')).toEqual(['Send this comment again', 'Send this reply again']);
		expect(titles('ambiguous')).toEqual([
			'This may send the comment twice if the provider received it before the connection failed',
			'This may send the reply twice if the provider received it before the connection failed',
		]);
	});

	it('drops the Retry, and only the Retry, of a send whose Retry was refused for good', () => {
		const refused = { ...comment('failed', [answer('reply-refused', 'ambiguous'), answer('reply-open', 'failed')]), user_message_id: 'root-refused' } as ChatComment;
		retryRefusedForGood('root-refused');
		retryRefusedForGood('reply-refused');
		expect(shown(unsentSends(refused)).map(([id, , failed, retry]) => [id, failed, retry])).toEqual([
			['root-refused', true, null], ['reply-refused', true, null], ['reply-open', true, 'Retry'],
		]);
		expect(needsAttention(refused)).toBe(true);
	});

	it('marks the comment for attention only where chat would put its red mark', () => {
		expect(needsAttention(comment('cancelled'))).toBe(false);
		expect(needsAttention(comment('delivered', [answer('broken', 'ambiguous')]))).toBe(true);
	});
});

// A queued input is held, not in flight, when the server proved no write happened and no turn owns it; the
// thread then says what it waits on, as chat does, in words for a comment or a reply.
describe('held chat comment sends', () => {
	const held = (delivery_error: string | null, extra: Partial<ChatComment> = {}) =>
		({ ...comment('queued', [], delivery_error), write_outcome: 'proved_no_write', ...extra }) as ChatComment;
	const heldReply = (delivery_error: string) =>
		({ ...answer('reply', 'queued'), delivery_error, write_outcome: 'proved_no_write' }) as ChatComment['answers'][number];
	const titled = (sends: ReturnType<typeof unsentSends>) => sends.map(send => [send.delivery.label, send.delivery.title, send.delivery.failed, send.retry?.label ?? null]);

	it('shows a comment held for a model choice, an account or a model with chat’s label and its own title', () => {
		expect(titled(unsentSends(held('no_model_selected')))).toEqual([['Choose a model to send', 'This comment is waiting for a model. Choose one for this chat to send it.', false, null]]);
		expect(titled(unsentSends(held('account_signed_out')))).toEqual([['Waiting for account', 'This comment sends when the account is available.', false, null]]);
		expect(titled(unsentSends(held('selected_model_unavailable')))).toEqual([['Waiting for model', 'This comment sends when the model is available.', false, null]]);
		expect(needsAttention(held('no_model_selected'))).toBe(false);
	});

	it('names a reply in a held reply’s title', () => {
		expect(titled(unsentSends(comment('delivered', [heldReply('selected_model_unavailable')])))).toEqual([['Waiting for model', 'This reply sends when the model is available.', false, null]]);
	});

	it('shows a hold with no comment wording by its label alone', () => {
		expect(titled(unsentSends(held('project_cloud_off')))).toEqual([['Switch to a local model to send', 'Switch to a local model to send', false, null]]);
	});

	it('shows a hold chat has no copy for as chat labels it, with no Retry', () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		expect(titled(unsentSends(held('some_new_hold')))).toEqual([['Waiting to send', 'Waiting to send', false, null]]);
		warn.mockRestore();
	});

	it('shows a send parked on a CORSIA defect as Not sent, marked and with Retry', () => {
		const parked = { ...comment('queued'), delivering_reason: 'delivery_defect' } as ChatComment;
		expect(titled(unsentSends(parked))).toEqual([['Not sent', 'Couldn’t send this comment. Try again.', true, 'Retry']]);
		expect(needsAttention(parked)).toBe(true);
		const parkedReply = { ...answer('reply', 'queued'), delivering_reason: 'delivery_defect' } as ChatComment['answers'][number];
		expect(titled(unsentSends(comment('delivered', [parkedReply])))).toEqual([['Not sent', 'Couldn’t send this reply. Try again.', true, 'Retry']]);
	});

	it('shows nothing for a queued send in flight: one a turn owns, or one with no proved hold', () => {
		expect(unsentSends(held('selected_model_unavailable', { active_turn_id: 'turn-1' }))).toEqual([]);
		expect(unsentSends(comment('queued'))).toEqual([]);
	});
});
