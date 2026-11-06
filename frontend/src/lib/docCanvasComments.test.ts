import { describe, expect, it } from 'vitest';
import { awaitsFirstMark, canvasCommentHighlights, canvasCommentRange, canvasCommentScrollTarget, canvasFocusedCommentScrollTarget, commentSurfaceStatus, commentsInDocumentOrder } from './docCanvasComments';
import { agentColor } from './docPresence';
import type { DocComment } from './stores/docComments';

const comment = (id: string, from: number, anchor: string, overrides: Partial<DocComment> = {}): DocComment => ({
	id, from, to: from + anchor.length, anchor, author: 'you', text: id, at: 'now', resolved: false, replies: [], ...overrides,
});
describe('canvas comment order and navigation', () => {
	it('orders by surviving text rather than open/resolved status and never finds a substitute quote', () => {
		const text = 'first second first';
		const rows = commentsInDocumentOrder([
			comment('later', 13, 'first'), comment('missing', 40, 'first'),
			comment('resolved', 0, 'first', { resolved: true }), comment('middle', 6, 'second'),
		], text);
		expect(rows.map((row) => row.id)).toEqual(['resolved', 'middle', 'later', 'missing']);
		expect(rows[3].orphaned).toBe(true);
		expect(canvasCommentHighlights(rows, text, 'resolved', undefined).map((range) => range.id)).toEqual(['resolved', 'middle', 'later']);
	});
	it('preserves an explicit scroll origin with the canonical comment range', () => {
		const text = 'first second';
		const rows = commentsInDocumentOrder([comment('second', 6, 'second')], text);

		const saved = canvasCommentScrollTarget(rows, text, 'second', false, 'comment-save');
		expect(saved).toEqual({ commentId: 'second', from: 6, to: 12, focus: false, origin: 'comment-save' });
		expect(canvasFocusedCommentScrollTarget(saved, rows, text, 'second', true)).toBe(saved);
		expect(canvasFocusedCommentScrollTarget(null, rows, text, 'second', true)).toEqual({ commentId: 'second', from: 6, to: 12, focus: true, origin: 'navigation' });
		expect(canvasFocusedCommentScrollTarget(saved, rows, text, 'missing', true)).toBeNull();
		expect(canvasCommentScrollTarget(rows, text, 'missing', true, 'navigation')).toBeNull();
	});
	it('leaves image-region focus in the image while preserving explicit text fallback navigation', () => {
		const text = '![image](image.jpg)';
		const rows = [comment('pin', 0, text, { imageSrc: 'image.jpg', region: { x: 0.3, y: 0.4, w: 0, h: 0 }, regionNumber: 1 })];
		expect(canvasFocusedCommentScrollTarget(null, rows, text, 'pin', false)).toBeNull();
		const fallback = canvasCommentScrollTarget(rows, text, 'pin', false, 'navigation');
		expect(fallback).toMatchObject({ from: 0, to: text.length, commentId: 'pin' });
		expect(canvasFocusedCommentScrollTarget(fallback, rows, text, 'pin', false)).toBe(fallback);
	});
	it('keeps unanchored review and orphaned cards after located cards with stable ties', () => {
		const rows = commentsInDocumentOrder([
			comment('b', 0, 'text'), comment('review', -1, '', { review: true }), comment('a', 0, 'text'),
		], 'text');
		expect(rows.map((row) => row.id)).toEqual(['a', 'b', 'review']);
		expect(rows[2].orphaned).toBe(false);
	});
});

describe('orphaned is the text on screen, plus a refused accept', () => {
	it.each([
		{ server: false, onScreen: true, refused: false, orphaned: false },
		{ server: false, onScreen: false, refused: false, orphaned: true },
		// The server's answer from a load describes the saved file, which can lag unsaved typing: it is not an input.
		{ server: true, onScreen: true, refused: false, orphaned: false },
		{ server: true, onScreen: false, refused: false, orphaned: true },
		// A refused accept on a stale screen: the quote is still shown, but the saved file no longer has it.
		{ server: false, onScreen: true, refused: true, orphaned: true },
		{ server: false, onScreen: false, refused: true, orphaned: true },
	])('server orphaned $server, quote on screen $onScreen, refused $refused: orphaned $orphaned', ({ server, onScreen, refused, orphaned }) => {
		const text = onScreen ? 'keep quote here' : 'keep other here';
		const rows = commentsInDocumentOrder([comment('card', 5, 'quote', { orphaned: server }), comment('located', 11, 'here')], text, refused ? { card: 'Refused' } : {});
		const row = rows.find((item) => item.id === 'card')!;
		expect(row.orphaned).toBe(orphaned);
		// The card, the mark, the jump and the order agree: an orphaned card has no place in the text.
		// Except that a comment whose text is gone from the document keeps a marker at the line it was on.
		expect(canvasCommentHighlights([row], text, null, undefined)).toHaveLength(orphaned && !row.detached ? 0 : 1);
		expect(canvasCommentRange(row, text)).toEqual(orphaned ? null : { from: 5, to: 10 });
		expect(rows.map((item) => item.id)).toEqual(orphaned ? ['located', 'card'] : ['card', 'located']);
	});
	it('never calls a review comment orphaned', () => {
		const [row] = commentsInDocumentOrder([comment('review', -1, '', { review: true, orphaned: true })], 'text');
		expect(row.orphaned).toBe(false);
	});
	it.each([
		{ name: 'accepted suggestion', overrides: { replacement: 'other', decision: 'accepted' as const } },
		{ name: 'rejected suggestion', overrides: { replacement: 'other', decision: 'rejected' as const } },
		{ name: 'resolved comment', overrides: {} },
	])('never counts a resolved $name whose quote is gone as unplaced', ({ overrides }) => {
		// A decision resolves its suggestion on the server; the header count is the list's open 'Text changed' group.
		const rows = commentsInDocumentOrder([comment('done', 5, 'quote', { ...overrides, resolved: true }), comment('lost', 5, 'quote')], 'keep other here');
		expect(commentSurfaceStatus(rows).orphanedCount).toBe(1);
	});
});

describe('live placement on the canvas (4.2)', () => {
	it('never draws or orphans a row that has not been placed, and lists it in server-offset order', () => {
		const text = 'alpha beta gamma';
		const rows = commentsInDocumentOrder([
			comment('placing', 0, 'alpha', { placement: 'placing', drawn: false }),
			comment('gamma', 11, 'gamma', { placement: 'placed', drawn: true }),
			comment('failed', 8, 'beta', { placement: 'not_placed', drawn: false }),
		], text);
		expect(rows.map((row) => [row.id, row.orphaned])).toEqual([['placing', false], ['failed', false], ['gamma', false]]);
		expect(canvasCommentRange(rows[0], text)).toBeNull();
		expect(canvasCommentHighlights(rows, text, null, () => {}).map((range) => range.id)).toEqual(['gamma']);
	});

	it('keeps drawing a row that was drawn while it is Placing or Not placed', () => {
		const text = 'alpha beta gamma';
		const rows = commentsInDocumentOrder([
			comment('moving', 6, 'beta', { placement: 'placing', drawn: true }),
			comment('kept', 11, 'gamma', { placement: 'not_placed', drawn: true }),
		], text);
		expect(canvasCommentHighlights(rows, text, null, () => {}).map((range) => range.id)).toEqual(['moving', 'kept']);
	});
});

describe('inline suggestions in the highlight ranges', () => {
	const text = 'Before. The team met on Tuesday and left. After.';
	const quote = 'The team met on Tuesday and left.';
	const suggestion = (id: string, from: number, anchor: string, overrides: Partial<DocComment> = {}): DocComment =>
		comment(id, from, anchor, { author: 'Reviewer', authorKind: 'agent', authorThreadId: `thread-${id}`, replacement: 'The team left.', ...overrides });
	const highlights = (rows: DocComment[], focusedId: string | null = null, accepting: string | null = null) =>
		canvasCommentHighlights(commentsInDocumentOrder(rows, text), text, focusedId, undefined, accepting);

	it('rides an open suggestion on its range with the quote, the insert and the agent presence colour', () => {
		const [range] = highlights([suggestion('s1', 8, quote)]);
		expect(range).toMatchObject({ id: 's1', from: 8, to: 8 + quote.length, suggestion: { quote, insert: 'The team left.', color: agentColor('thread-s1'), inline: true, busy: false } });
	});

	it('falls back to the author name for the colour when the row has no thread', () => {
		const [range] = highlights([suggestion('s1', 8, quote, { authorThreadId: null })]);
		expect(range.suggestion?.color).toBe(agentColor('Reviewer'));
	});

	it('leaves a plain comment without a suggestion', () => {
		expect(highlights([comment('c1', 0, 'Before.')])[0].suggestion).toBeUndefined();
	});

	it('keeps the earliest of overlapping suggestions inline and the rest card-only', () => {
		const rows = [
			suggestion('b', 12, 'team met on Tuesday', { createdAt: '2' }),
			suggestion('a', 8, quote, { createdAt: '1' }),
			suggestion('c', 8 + quote.length + 1, 'After.', { createdAt: '3' }),
		];
		expect(highlights(rows).map((range) => [range.id, range.suggestion?.inline])).toEqual([['a', true], ['b', false], ['c', true]]);
	});

	it('does not count a suggestion that only touches the earlier one as overlapped', () => {
		const rows = [suggestion('a', 8, 'The team met'), suggestion('b', 20, ' on Tuesday')];
		expect(highlights(rows).map((range) => range.suggestion?.inline)).toEqual([true, true]);
	});

	it('marks only the suggestion whose Accept is in flight as busy', () => {
		const rows = [suggestion('a', 8, 'The team met'), suggestion('b', 21, 'on Tuesday')];
		expect(highlights(rows, null, 'b').map((range) => range.suggestion?.busy)).toEqual([false, true]);
	});

	it('gives a pure deletion an empty insert', () => {
		expect(highlights([suggestion('s1', 8, quote, { replacement: '' })])[0].suggestion).toMatchObject({ insert: '' });
	});

	it('draws nothing for a resolved suggestion', () => {
		expect(highlights([suggestion('s1', 8, quote, { resolved: true })])).toEqual([]);
	});

	it.each([
		['accepted', { decision: 'accepted' as const }],
		['rejected', { decision: 'rejected' as const }],
	])('draws a %s suggestion that is still open as a plain comment mark, with no inline suggestion', (_name, overrides) => {
		expect(highlights([suggestion('s1', 8, quote, overrides)])).toMatchObject([{ id: 's1', from: 8, to: 8 + quote.length, suggestion: undefined }]);
	});

	it('marks a suggestion whose quote no longer matches the text on screen at the line it was on, not as a passage', () => {
		const rows = commentsInDocumentOrder([suggestion('s1', 8, quote)], 'Before. The team changed its mind. After.');
		expect(rows[0].orphaned).toBe(true);
		expect(canvasCommentHighlights(rows, 'Before. The team changed its mind. After.', null, undefined)).toMatchObject([{ id: 's1', from: 8, to: 8, point: true, lost: true }]);
	});
});

describe('comment surface status', () => {
	it('counts orphaned cards and reports the placement reload state', () => {
		expect(commentSurfaceStatus([])).toEqual({ commentCount: 0, orphanedCount: 0, replaceableCount: 0, placementReloading: false, placementReloadError: false });
		expect(commentSurfaceStatus([
			comment('a', 0, 'x', { orphaned: true }), comment('b', 0, 'x', { placementReloading: true }), comment('c', 0, 'x', { placementReloadError: true }),
		])).toEqual({ commentCount: 3, orphanedCount: 1, replaceableCount: 1, placementReloading: true, placementReloadError: true });
	});

	it('offers Place again only for orphaned comments whose quote remains in the text', () => {
		const deleted = commentsInDocumentOrder([comment('deleted', 0, 'quote')], 'other');
		const restored = commentsInDocumentOrder([comment('restored', 8, 'quote')], 'restored quote', { restored: 'Refused' });
		const elsewhere = commentsInDocumentOrder([comment('elsewhere', 20, 'quote')], 'quote elsewhere');
		const refused = commentsInDocumentOrder([comment('refused', 8, 'quote')], 'refused quote', { refused: 'Refused' });

		expect(deleted[0].detached).toBe(true);
		expect(commentSurfaceStatus(deleted)).toMatchObject({ orphanedCount: 1, replaceableCount: 0 });
		for (const rows of [restored, elsewhere, refused]) {
			expect(rows[0].detached).toBe(false);
			expect(commentSurfaceStatus(rows)).toMatchObject({ orphanedCount: 1, replaceableCount: 1 });
		}
	});

	it('counts only open threads for the header pill', () => {
		expect(commentSurfaceStatus([comment('a', 0, 'x'), comment('b', 0, 'x', { resolved: true }), comment('c', 0, 'x', { resolved: true, orphaned: true })]).commentCount).toBe(1);
	});
});

describe('awaitsFirstMark', () => {
	it('holds a pending mark only for a comment that can still be drawn', () => {
		expect(awaitsFirstMark(comment('a', 0, 'x', { placement: 'placing' }), false)).toBe(true);
		expect(awaitsFirstMark(comment('a', 0, 'x', { placement: 'placed' }), true)).toBe(false);
		expect(awaitsFirstMark(comment('a', 0, 'x', { placement: 'not_placed' }), false)).toBe(false);
		expect(awaitsFirstMark(comment('a', 0, 'x', { orphaned: true }), false)).toBe(false);
		expect(awaitsFirstMark(undefined, false)).toBe(false);
	});
});

describe('a comment whose text was deleted keeps a marker at the nearest line', () => {
	const text = 'Lunch is at noon.\nTrain leaves at nine.';
	const gone = (overrides: Partial<DocComment> = {}) => comment('gone', 18, 'two extra liters', { to: 18, drawn: true, placement: 'placed', ...overrides });

	it('reads as detached when the text is nowhere in the document', () => {
		const [row] = commentsInDocumentOrder([comment('gone', 18, 'two extra liters')], text);
		expect(row).toMatchObject({ orphaned: true, detached: true });
	});

	it('reads as detached when everything inside its range was deleted', () => {
		const [row] = commentsInDocumentOrder([gone({ anchor: 'Train' })], text);
		expect(row).toMatchObject({ orphaned: true, detached: true });
	});

	it('draws a point marker flagged lost at the position the text was, with its reply-waiting dot', () => {
		const replies = [{ id: 'r', author: 'Reviewer', text: 'Why?', at: 'now' }];
		const rows = commentsInDocumentOrder([gone({ replies, newReplyIds: ['r'] })], text);
		expect(canvasCommentHighlights(rows, text, null, undefined)).toMatchObject([{ id: 'gone', from: 18, to: 18, point: true, lost: true, replyWaiting: true }]);
	});

	it('draws no marker for a resolved comment, a review comment or one with no known position', () => {
		const rows = commentsInDocumentOrder([gone({ resolved: true }), gone({ id: 'r', review: true }), gone({ id: 'n', from: undefined, to: undefined })], text);
		expect(canvasCommentHighlights(rows, text, null, undefined)).toEqual([]);
	});

	it('clamps the marker to the end of a document that got shorter', () => {
		const rows = commentsInDocumentOrder([gone({ from: 500, to: 500 })], text);
		expect(canvasCommentHighlights(rows, text, null, undefined)[0]).toMatchObject({ from: text.length, to: text.length, point: true });
	});

	it('follows an edit inside its range to the words now inside it', () => {
		const edited = 'Lunch is at noon.\nTrain leaves at ten.';
		const [row] = commentsInDocumentOrder([comment('live', 18, 'Train leaves at nine.', { drawn: true, placement: 'placed', to: 18 + 'Train leaves at ten.'.length })], edited);
		expect(row).toMatchObject({ anchor: 'Train leaves at ten.', orphaned: false });
	});
});

describe('a decided suggestion that has replies stays an open conversation', () => {
	const after = 'Lunch is at noon. Train leaves at ten.';
	const accepted = (overrides: Partial<DocComment> = {}): DocComment =>
		comment('s1', 34, 'nine', { replacement: 'ten', decision: 'accepted', author: 'Reviewer', replies: [{ id: 'r', author: 'Reviewer', text: 'Thanks', at: 'now' }] as DocComment['replies'], ...overrides });

	it('draws a margin marker at the accepted text rather than nothing', () => {
		const rows = commentsInDocumentOrder([accepted()], after);
		expect(canvasCommentHighlights(rows, after, null, undefined)).toMatchObject([{ id: 's1', from: 34, to: 34, point: true, lost: false }]);
	});

	it('draws nothing once it is resolved', () => {
		const rows = commentsInDocumentOrder([accepted({ resolved: true })], after);
		expect(canvasCommentHighlights(rows, after, null, undefined)).toEqual([]);
	});

	it('is not counted as text that could not be placed', () => {
		const rows = commentsInDocumentOrder([accepted()], after);
		expect(commentSurfaceStatus(rows)).toMatchObject({ orphanedCount: 0, replaceableCount: 0, commentCount: 1 });
	});

	it('does not read as orphaned while its own Accept is in flight and the replacement already stands where the quote was', () => {
		const open = accepted({ decision: undefined, replies: [] });
		const [row] = commentsInDocumentOrder([open], after, {}, 's1');
		expect(row.orphaned).toBe(false);
		const [other] = commentsInDocumentOrder([open], after);
		expect(other.orphaned).toBe(true);
	});
});
