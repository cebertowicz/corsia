import { describe, expect, it } from 'vitest';
import { unreadCommentCount, unreadReplyCount } from './docUnread';
import type { DocComment } from '$lib/stores/docComments';

const thread = (id: string, newReplyIds: string[] = [], extra: Partial<DocComment> = {}): DocComment =>
	({ id, anchor: '', author: 'you', text: '', at: '', resolved: false, replies: [], newReplyIds, ...extra });

describe('unreadCommentCount', () => {
	it("counts replies, not threads, as the jump control's label does", () => {
		expect(unreadCommentCount([thread('a', ['r1', 'r2']), thread('b'), thread('c', ['r3'])])).toBe(3);
	});

	it('is zero when nothing is waiting, and for a thread with no stamp at all', () => {
		expect(unreadCommentCount([thread('a'), { ...thread('b'), newReplyIds: undefined }])).toBe(0);
		expect(unreadCommentCount([])).toBe(0);
	});

	it('counts an unread root comment beside its replies, a suggestion included', () => {
		expect(unreadCommentCount([
			thread('a', ['r1'], { newComment: true }),
			thread('s', [], { newComment: true, replacement: 'so it reads kindly' }),
			thread('s2', ['r2'], { replacement: 'and here' }),
		])).toBe(4);
	});
});

describe('unreadReplyCount', () => {
	it('counts unread replies in threads the person commented in', () => {
		expect(unreadReplyCount([
			thread('mine', ['r1', 'r2'], { authorKind: 'user' }),
			thread('asked', ['r3'], {
				authorKind: 'agent',
				replies: [{ author: 'me', authorKind: 'user', text: 'answered', at: '' }],
			}),
		])).toBe(3);
	});

	it('leaves out unread top-level comments and replies in threads the person never entered', () => {
		const threads = [
			thread('tagged', [], { authorKind: 'agent', newComment: true }),
			thread('directed', ['r1'], { authorKind: 'agent' }),
		];
		// Activity's row and the open document's bar speak only of replies to the person's comments;
		// the jump control's whole-unread set still counts both.
		expect(unreadReplyCount(threads)).toBe(0);
		expect(unreadCommentCount(threads)).toBe(2);
	});
});
