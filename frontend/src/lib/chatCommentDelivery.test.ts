import { describe, expect, it } from 'vitest';
import type { ChatComment, Message } from '$lib/stores/threads';
import { chatReplyIndex, suppressibleCommentEnvelopes } from './chatCommentDelivery';

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
});

describe('pending comment replies', () => {
	it('shows optimistic and queued replies for the matching comment until they are delivered or fail', () => {
		const optimistic = { ...row('optimistic', 'queued'), delivery_state: undefined, _optimistic: true } as Message;
		const queued = row('queued', 'queued');
		const rows = [optimistic, queued, row('delivered', 'delivered'), row('failed', 'failed'), row('other', 'queued', 'other')];
		expect(chatReplyIndex(rows, 'thread').pending(root).map(message => message.id)).toEqual(['optimistic', 'queued']);
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

describe('comment-only envelopes', () => {
	it.each(['ambiguous', 'cancelled'] as const)('hides a %s send even when an answer is loaded', state => {
		const send = row('send', state);
		const target = { id: 'target', thread_id: 'thread', role: 'assistant', content: 'quote', chat_comments: [{ ...root, answers: [{ author_kind: 'user', user_message_id: 'send', cid: 'reply-cid' }] }] } as unknown as Message;
		expect(suppressibleCommentEnvelopes([target, send], 'thread').has('send')).toBe(true);
	});
	it('hides comment-only sends throughout failure and retry', () => {
		const target = { id: 'target', thread_id: 'thread', role: 'assistant', content: 'quote', chat_comments: [{ ...root, answers: [] }] } as unknown as Message;
		const failed = row('failed', 'failed');
		const queued = { ...row('queued', 'queued'), metadata: { ...row('queued', 'queued').metadata, retried_from_message_id: 'failed' } } as Message;
		expect([...suppressibleCommentEnvelopes([target, failed, queued], 'thread')]).toEqual(['failed', 'queued']);
		const retry = { ...queued, delivery_state: 'failed' } as Message;
		expect([...suppressibleCommentEnvelopes([target, failed, retry], 'thread')]).toEqual(['failed', 'queued']);
	});
	// Whether this envelope is hidden when the transcript holds it alongside these rows.
	const placed = (envelope: Message, rows: Message[]) =>
		suppressibleCommentEnvelopes([envelope, ...rows.filter(other => other.id !== envelope.id)], envelope.thread_id).has(envelope.id);

	it('hides a pending reply whether its original thread is loaded', () => {
		const pending = { ...row('pending', 'queued'), _optimistic: true } as Message;
		const target = { id: 'target', thread_id: 'thread', role: 'assistant', content: 'quote', chat_comments: [{ ...root, answers: [] }] } as unknown as Message;
		expect(placed(pending, [pending])).toBe(true);
		expect(placed(pending, [pending, target])).toBe(true);
		expect(placed({ ...pending, content: 'Also do this' }, [pending, target])).toBe(false);
		const failed = { ...pending, _optimistic: false, delivery_state: 'failed' } as Message;
		expect(placed(failed, [failed, target])).toBe(true);
		expect(placed(failed, [failed])).toBe(true);
	});

	it('hides regardless of whether comments are available on a loaded target', () => {
		const input = row('send', 'failed');
		const target = { id: 'target', thread_id: 'thread', role: 'assistant', content: 'quote', chat_comments: [{ ...root, answers: [{ author_kind: 'user', user_message_id: 'send', cid: 'reply-cid' }] }] } as unknown as Message;
		expect(placed(input, [input])).toBe(true);
		expect(placed(input, [input, target])).toBe(true);

		const retry = { ...input, id: 'retry', metadata: { ...input.metadata, retried_from_message_id: input.id, reply_context: { comments: [{ id: 'reply-id', cid: 'retry-cid' }] } } } as Message;
		const retriedTarget = { ...target, chat_comments: [{ ...root, answers: [{ author_kind: 'user', user_message_id: 'retry', cid: 'retry-cid' }] }] } as unknown as Message;
		expect(placed(input, [input, retry, retriedTarget])).toBe(true);
		expect(placed({ ...input, content: 'Also do this' }, [target])).toBe(false);
		expect(placed({ ...input, metadata: { ...input.metadata, attachments: [{ name: 'file' }] } } as Message, [target])).toBe(false);
	});
});
