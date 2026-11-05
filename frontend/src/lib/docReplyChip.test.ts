import { describe, expect, it } from 'vitest';
import { nextUnreadThread, unreadReplyCount } from './docReplyChip';
import type { DocComment } from '$lib/stores/docComments';

const thread = (id: string, newReplyIds: string[] = []): DocComment => ({ id, anchor: '', author: 'you', text: '', at: '', resolved: false, replies: [], newReplyIds });

describe('unreadReplyCount', () => {
	it('counts replies, not threads, as the chat chip does', () => {
		expect(unreadReplyCount([thread('a', ['r1', 'r2']), thread('b'), thread('c', ['r3'])])).toBe(3);
	});

	it('is zero when nothing is waiting, and for a thread with no stamp at all', () => {
		expect(unreadReplyCount([thread('a'), { ...thread('b'), newReplyIds: undefined }])).toBe(0);
		expect(unreadReplyCount([])).toBe(0);
	});
});

describe('nextUnreadThread', () => {
	const order = [thread('a', ['r1']), thread('b'), thread('c', ['r2']), thread('d', ['r3'])];

	it('starts at the first waiting thread in document order', () => {
		expect(nextUnreadThread(order, null)?.id).toBe('a');
	});

	it('steps forward, skipping read threads, then comes back to the first', () => {
		expect(nextUnreadThread(order, 'a')?.id).toBe('c');
		expect(nextUnreadThread(order, 'c')?.id).toBe('d');
		expect(nextUnreadThread(order, 'd')?.id).toBe('a');
	});

	it('stays on the only waiting thread, so a tap before it is read still lands on it', () => {
		expect(nextUnreadThread([thread('a', ['r1'])], 'a')?.id).toBe('a');
	});

	it('skips the visible thread without losing its position in document order', () => {
		expect(nextUnreadThread(order, 'c', 'c')?.id).toBe('d');
		expect(nextUnreadThread(order, 'd', 'a')?.id).toBe('c');
		expect(nextUnreadThread([thread('a', ['r1'])], null, 'a')).toBeNull();
	});

	it('keeps moving forward when the thread it left has cleared', () => {
		const cleared = [thread('a', ['r1']), thread('b'), thread('c', ['r2'])];
		expect(nextUnreadThread(cleared, 'b')?.id).toBe('c');
	});

	it('starts over when the thread it left is gone from the document', () => {
		expect(nextUnreadThread(order, 'gone')?.id).toBe('a');
	});

	it('is none when nothing is waiting', () => {
		expect(nextUnreadThread([thread('a'), thread('b')], 'a')).toBeNull();
	});
});
