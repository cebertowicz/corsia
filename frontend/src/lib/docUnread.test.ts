import { describe, expect, it } from 'vitest';
import { unreadCommentCount } from './docUnread';
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
