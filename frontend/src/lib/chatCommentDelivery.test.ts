import { describe, expect, it } from 'vitest';
import type { ChatComment, Message } from '$lib/stores/threads';
import { chatReplyIndex } from './chatCommentDelivery';

const root = { id: 'root', user_message_id: 'author', cid: 'c1' } as ChatComment;
const row = (id: string, state: string, sender = 'author', cid = 'c1', thread = 'thread') => ({
	id, thread_id: thread, role: 'user', content: '', created_at: 'now', delivery_state: state,
	metadata: { reply_context: { comments: [{ id: 'reply-id', cid: 'reply-cid', root_user_message_id: sender, root_cid: cid, note: id }] } },
}) as Message;

describe('failed comment replies', () => {
	it('matches the failed send to its original comment and thread', () => {
		const rows = [row('failed', 'failed'), row('delivered', 'delivered'), row('other-author', 'failed', 'other'), row('other-comment', 'failed', 'author', 'c2'), row('other-thread', 'failed', 'author', 'c1', 'elsewhere')];
		expect(chatReplyIndex(rows, 'thread').failed(root).map(message => message.id)).toEqual(['failed']);
	});

	it('clears a failure only after its retry is delivered', () => {
		const failed = row('failed', 'failed');
		const retry = { ...row('retry', 'delivered'), metadata: { ...row('retry', 'delivered').metadata, retried_from_message_id: 'failed' } } as Message;
		expect(chatReplyIndex([failed, retry], 'thread').failed(root)).toEqual([]);
		const failedRetry = { ...retry, delivery_state: 'failed' } as Message;
		expect(chatReplyIndex([failed, failedRetry], 'thread').failed(root)).toEqual([failedRetry]);
	});

	it('gives a root comment whose own send failed after it was stored its failure in its own thread', () => {
		const own = (id: string, state: string) => ({ id, thread_id: 'thread', role: 'user', content: '', created_at: 'now', delivery_state: state,
			metadata: { reply_context: { comments: [{ id: 'root', cid: 'c1', note: 'my comment' }] } } }) as Message;
		expect(chatReplyIndex([own('author', 'failed')], 'thread').failed(root).map(message => message.id)).toEqual(['author']);
		expect(chatReplyIndex([own('author', 'queued')], 'thread').failed(root)).toEqual([]);
		expect(chatReplyIndex([own('author', 'queued')], 'thread').pending(root)).toEqual([]);
		expect(chatReplyIndex([own('other', 'failed')], 'thread').failed(root)).toEqual([]);
	});

	it('finds a root comment\'s failed send by the comment\'s id, before the sender knows its cid', () => {
		const sent = { id: 'author', thread_id: 'thread', role: 'user', content: '', created_at: 'now', delivery_state: 'failed',
			metadata: { reply_context: { comments: [{ id: 'root', note: 'my comment' }] } } } as Message;
		expect(chatReplyIndex([sent], 'thread').failed(root).map(message => message.id)).toEqual(['author']);
		expect(chatReplyIndex([sent], 'thread').failed({ ...root, id: 'another' }).map(message => message.id)).toEqual([]);
	});
});

describe('pending comment replies', () => {
	it('shows stored, queued replies for the matching comment until they are delivered or fail', () => {
		// A send the server has not stored yet stays in the box it was typed in.
		const optimistic = { ...row('optimistic', 'queued'), delivery_state: undefined, _optimistic: true } as Message;
		const queued = row('queued', 'queued');
		const rows = [optimistic, queued, row('delivered', 'delivered'), row('failed', 'failed'), row('other', 'queued', 'other')];
		expect(chatReplyIndex(rows, 'thread').pending(root).map(message => message.id)).toEqual(['queued']);
		expect(chatReplyIndex(rows, 'elsewhere').pending(root)).toEqual([]);
	});

	it('suppresses only the pending send already projected as a user answer', () => {
		const pending = row('send', 'queued');
		const other = row('other-send', 'queued');
		pending.metadata.reply_context.comments[0].note = 'same text';
		other.metadata.reply_context.comments[0].note = 'same text';
		const projected = { ...root, answers: [{ author_kind: 'user', user_message_id: 'send', cid: 'reply-cid', text: 'same text' }] } as ChatComment;
		expect(chatReplyIndex([pending, other], 'thread').pending(projected).map(message => message.id)).toEqual(['other-send']);
		expect(chatReplyIndex([pending], 'thread').pending({ ...projected, answers: [{ author_kind: 'agent', user_message_id: 'send', cid: 'reply-cid', text: 'same text' }] } as ChatComment)).toEqual([pending]);
	});
});
