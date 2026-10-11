import { describe, expect, it } from 'vitest';
import type { DocComment } from '$lib/stores/docComments';
import { commentsAtImage, groupRows, imageGroupKey, remapOccurrence, sharedImage } from './docImageComments';

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
function comment(id: string, extra: Partial<DocComment> = {}): DocComment {
	return { id, anchor: '![Mock](a.png)', author: 'you', authorKind: 'user', text: id, at: 'now', resolved: false, replies: [], ...extra };
}
const onImage = (id: string, number: number, from = 10, to = 24, extra: Partial<DocComment> = {}) =>
	comment(id, { region: BOX, regionNumber: number, imageSrc: '/ws/a.png', from, to, ...extra });

describe('comments on images inside a document', () => {
	it('finds the region comments on one image, not on another showing the same file, nor text comments', () => {
		const comments = [onImage('one', 1), onImage('two', 2), onImage('later', 3, 40, 54), comment('text', { from: 10, to: 24 }), onImage('gone', 4, 10, 24, { orphaned: true })];
		expect(commentsAtImage(comments, { from: 10, to: 24 }).map((c) => c.id)).toEqual(['one', 'two']);
		expect(commentsAtImage(comments, { from: 40, to: 54 }).map((c) => c.id)).toEqual(['later']);
	});

	it('names the image a marker opens only when all its comments are region comments on that one image', () => {
		const comments = [onImage('one', 1), onImage('two', 2), onImage('later', 3, 40, 54), comment('text', { from: 10, to: 24 })];
		expect(sharedImage(comments, ['one', 'two'])).toEqual({ from: 10, to: 24 });
		expect(sharedImage(comments, ['one', 'later'])).toBeNull();
		expect(sharedImage(comments, ['one', 'text'])).toBeNull();
		expect(sharedImage(comments, ['missing'])).toBeNull();
	});

	it('groups each image\'s comments by its file and place, and a detached one by its file alone', () => {
		expect(imageGroupKey(onImage('one', 1))).toBe('/ws/a.png\u000010');
		expect(imageGroupKey(onImage('gone', 2, 10, 24, { orphaned: true }))).toBe('/ws/a.png\u0000detached');
		expect(imageGroupKey(comment('text'))).toBeNull();
		expect(imageGroupKey(comment('artifact', { region: BOX, regionNumber: 1 }))).toBeNull();
	});

	it('lists one image\'s comments together at the first one\'s place, in their number order', () => {
		const rows = groupRows(
			[comment('before'), onImage('b', 2), comment('between'), onImage('a', 1), onImage('c', 1, 40, 54)],
			imageGroupKey,
		);
		expect(rows.map((row) => row.comments.map((c) => c.id))).toEqual([['before'], ['a', 'b'], ['between'], ['c']]);
		expect(rows.map((row) => row.key)).toEqual([null, '/ws/a.png\u000010', null, '/ws/a.png\u000040']);
	});

	it('follows its markdown through an edit, and stays the same object when the edit is elsewhere', () => {
		const image = { from: 10, to: 24, src: 's' };
		expect(remapOccurrence(image, (pos) => pos + 5)).toEqual({ from: 15, to: 29, src: 's' });
		expect(remapOccurrence(image, (pos) => pos)).toBe(image);
	});
});
