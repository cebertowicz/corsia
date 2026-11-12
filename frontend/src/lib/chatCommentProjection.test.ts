import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { messages, type Message } from '$lib/stores/threads';
import { chatCommentInputs } from './chatCommentInputs.svelte';
import { emit } from './ws/events';

const comment = (id: string, state: string, content = '', attachments: unknown[] = [], extra: Record<string, unknown> = {}) => ({
	id, thread_id: 'thread', role: 'user', content, delivery_state: state, created_at: '2026-11-12T00:00:00Z',
	metadata: { reply_context: { comments: [{ id: 'one', note: 'note' }] }, attachments, ...extra },
}) as Message;
const answer = { id: 'answer', thread_id: 'thread', role: 'assistant', content: 'Answer', created_at: '2026-11-12T00:00:01Z' } as Message;

afterEach(() => messages.set([]));

describe('chat comment transcript projection', () => {
	// Every path into the transcript (history, a broadcast, a page load) goes through the store, so the store is what keeps comment traffic out of it.
	it.each(['queued', 'delivered', 'failed', 'ambiguous', 'cancelled'])('keeps a %s comment-only input out of the transcript, in its thread\'s store', state => {
		messages.set([comment(`set-${state}`, state), answer]);
		messages.update(rows => [...rows, comment(`update-${state}`, state)]);
		expect(get(messages).map(row => row.id)).toEqual(['answer']);
		expect(chatCommentInputs.rows('thread').filter(row => row.id.endsWith(state)).map(row => row.delivery_state)).toEqual([state, state]);
	});
	it('keeps typed text and attachments in the transcript', () => {
		messages.set([comment('text', 'delivered', 'also do this'), comment('file', 'delivered', '', [{}])]);
		expect(get(messages).map(row => row.id)).toEqual(['text', 'file']);
		expect(chatCommentInputs.rows('thread').some(row => row.id === 'text' || row.id === 'file')).toBe(false);
	});
	it('settles a stored input\'s later delivery on its one row, never an older one over a newer', () => {
		messages.update(rows => [...rows, { ...comment('settled', 'queued'), delivery_revision: 2 }]);
		messages.update(rows => [...rows, { ...comment('settled', 'failed'), delivery_revision: 3, delivery_error: 'Not delivered' }]);
		messages.update(rows => [...rows, { ...comment('settled', 'queued'), delivery_revision: 2 }]);
		const rows = chatCommentInputs.rows('thread').filter(row => row.id === 'settled');
		expect(rows.map(row => [row.delivery_state, row.delivery_error])).toEqual([['failed', 'Not delivered']]);
	});
	it('settles a post whose stored copy a page load brought first, without its input id', () => {
		const anchor = { from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: '' };
		chatCommentInputs.sending('early', 'thread', { id: 'c', seq: 1, ...anchor, note: 'note' });
		chatCommentInputs.postRoot('thread', 'm1', anchor, 'note', 'early', '');
		messages.set([{ ...comment('early', 'delivered'), metadata: { reply_context: { comments: [{ id: 'c', note: 'note' }] } } } as Message]);
		emit('input_accepted', { thread_id: 'thread', input_id: 'early', message_id: 'early', created_at: '2026-11-12T00:00:02Z' });
		expect(chatCommentInputs.root('thread', 'm1', anchor)).toBeUndefined();
		expect(chatCommentInputs.rows('thread').filter(row => row.id === 'early').map(row => row.metadata?.input_id)).toEqual(['early']);
	});
	it('leaves an unchanged transcript list as it is', () => {
		const rows = [answer];
		messages.set(rows);
		expect(get(messages)).toBe(rows);
	});
});
