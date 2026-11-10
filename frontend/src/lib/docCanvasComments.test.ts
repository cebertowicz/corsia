import { describe, expect, it } from 'vitest';
import { awaitsFirstMark, canvasCommentHighlights, canvasCommentRange, canvasCommentScrollTarget, canvasFocusedCommentScrollTarget, commentSurfaceStatus, commentsInDocumentOrder } from './docCanvasComments';
import { agentColor } from './docPresence';
import type { DocComment } from './stores/docComments';

const comment = (id: string, from: number, anchor: string, overrides: Partial<DocComment> = {}): DocComment => ({
	id, from, to: from + anchor.length, anchor, author: 'you', text: id, at: 'now', resolved: false, replies: [], ...overrides,
});
describe('local and server comment status', () => {
	it.each([
		{ text: 'The quick fox', end: 9, decision: null, orphaned: false, changed: false },
		{ text: 'The quXick fox', end: 10, decision: null, orphaned: false, changed: true },
		{ text: 'The  fox', end: 4, decision: null, orphaned: true, changed: false },
		{ text: 'The  fox', end: 4, decision: 'rejected' as const, orphaned: false, changed: false },
	])('matches the server case $text, decision $decision', ({ text, end, decision, orphaned, changed }) => {
		const base = comment('case', 4, 'quick', { to: end, replacement: 'slow', decision, drawn: true });
		const [local] = commentsInDocumentOrder([base], text);
		const [snapshot] = commentsInDocumentOrder([{ ...base, statusText: text, orphaned, textChanged: changed }], text);
		expect(local).toMatchObject({ orphaned, textChanged: changed });
		expect(snapshot).toMatchObject({ orphaned, textChanged: changed });
	});
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
	it('uses the server position after text is inserted before a legacy quote', () => {
		const text = 'New material before. The quoted source stays here.';
		const start = text.indexOf('quoted source');
		const rows = commentsInDocumentOrder([comment('legacy', start, 'quoted source', { orphaned: false, statusText: text })], text);
		expect(canvasCommentRange(rows[0], text)).toEqual({ from: start, to: start + 'quoted source'.length });
		expect(canvasCommentHighlights(rows, text, null, undefined)).toMatchObject([{ id: 'legacy', from: start, to: start + 'quoted source'.length }]);
	});

	it('marks the highlight of a thread with a reply or comment the reader has not opened, naming which is new', () => {
		const text = 'one two three four';
		const rows = commentsInDocumentOrder([
			comment('replied', 0, 'one', { newReplyIds: ['r1'] }), comment('started', 4, 'two', { newComment: true }), comment('read', 8, 'three', { newReplyIds: [] }),
			comment('both', 14, 'four', { newComment: true, newReplyIds: ['r2'] }),
		], text);
		expect(canvasCommentHighlights(rows, text, null, undefined).map((range) => [range.id, range.unread ?? false])).toEqual([['replied', 'reply'], ['started', 'comment'], ['read', false], ['both', 'reply']]);
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
	it('gives an image comment its pin on the image: region, number and its author colour; a whole-image comment has none', () => {
		const text = '![image](image.jpg)';
		const region = { x: 0.3, y: 0.4, w: 0, h: 0 };
		const rows = [comment('pin', 0, text, { imageSrc: 'image.jpg', region, regionNumber: 2 }), comment('whole', 0, text)];
		const colorOf = (row: DocComment) => (row.id === 'pin' ? '#123456' : '#000000');
		expect(canvasCommentHighlights(rows, text, null, undefined, null, colorOf).map((range) => [range.id, range.pin])).toEqual([
			['pin', { region, number: 2, color: '#123456' }], ['whole', undefined],
		]);
		expect(canvasCommentHighlights(rows, text, null, undefined)[0].pin).toBeUndefined();
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

	it('draws a rejected suggestion that is still open as a plain comment mark on its quote, with no inline suggestion', () => {
		expect(highlights([suggestion('s1', 8, quote, { decision: 'rejected' })])).toMatchObject([{ id: 's1', from: 8, to: 8 + quote.length, suggestion: undefined }]);
	});

	it('marks a suggestion whose quote no longer matches the text on screen at the line it was on, not as a passage', () => {
		const rows = commentsInDocumentOrder([suggestion('s1', 8, quote)], 'Before. The team changed its mind. After.');
		expect(rows[0].orphaned).toBe(true);
		expect(canvasCommentHighlights(rows, 'Before. The team changed its mind. After.', null, undefined)).toMatchObject([{ id: 's1', from: 8, to: 8, point: true }]);
	});
});

describe('comment surface status', () => {
	it('counts the open cards, orphaned ones included', () => {
		expect(commentSurfaceStatus([])).toEqual({ commentCount: 0 });
		expect(commentSurfaceStatus([
			comment('a', 0, 'x', { orphaned: true }), comment('b', 0, 'x'), comment('c', 0, 'x'),
		])).toEqual({ commentCount: 3 });
	});

	it('keeps the collapsed anchor of a lost comment at its last line when its quote returns', () => {
		const original = comment('deleted', 4, 'quote', { to: 4, drawn: true });
		const goneText = 'The  fox';
		const returnedText = 'The  fox; quote returned';
		const gone = commentsInDocumentOrder([original], goneText);
		const returned = commentsInDocumentOrder([original], returnedText);

		expect(gone[0]).toMatchObject({ orphaned: true, detached: true });
		expect(returned[0]).toMatchObject({ orphaned: true, detached: true });
		expect(canvasCommentHighlights(returned, returnedText, null, undefined)).toMatchObject([{ id: 'deleted', from: 4, to: 4, point: true }]);
	});

	it('counts only open threads for the header pill', () => {
		expect(commentSurfaceStatus([comment('a', 0, 'x'), comment('b', 0, 'x', { resolved: true }), comment('c', 0, 'x', { resolved: true, orphaned: true })]).commentCount).toBe(1);
	});
});

describe('a live-tracked comment keeps its two ends where the text moves them', () => {
	// "The quick brown fox", after "red " was typed inside a comment on "quick brown": the tracked range grew to cover it, its stored quote did not.
	const edited = 'The quick red brown fox';
	const tracked = (overrides: Partial<DocComment> = {}) => comment('c', 4, 'quick brown', { drawn: true, placement: 'placed', to: 19, ...overrides });

	it('keeps the thread on the words now inside after an edit inside its range', () => {
		const [row] = commentsInDocumentOrder([tracked()], edited);
		expect(row.orphaned).toBe(false);
		expect(row.anchor).toBe('quick red brown');
		expect(canvasCommentRange(row, edited)).toEqual({ from: 4, to: 19 });
		expect(canvasCommentHighlights([row], edited, null, undefined).map((range) => [range.id, range.from, range.to])).toEqual([['c', 4, 19]]);
	});

	it('leaves a comment the document does not track as it was', () => {
		const [row] = commentsInDocumentOrder([tracked({ drawn: false, placement: undefined })], edited);
		expect(row.orphaned).toBe(true);
	});

	it('keeps an open suggestion at its line after an edit inside, with its quote unchanged and no inline edit', () => {
		const [row] = commentsInDocumentOrder([tracked({ replacement: 'swift' })], edited);
		expect(row.orphaned).toBe(false);
		expect(row.textChanged).toBe(true);
		expect(row.anchor).toBe('quick brown');
		expect(canvasCommentRange(row, edited)).toEqual({ from: 4, to: 19 });
		expect(canvasCommentHighlights([row], edited, null, undefined).map((range) => [range.id, range.from, range.to, range.suggestion])).toEqual([['c', 4, 19, undefined]]);
	});

	it('keeps it after a reload too, where the server says the stored place still holds (and not otherwise)', () => {
		const [kept] = commentsInDocumentOrder([tracked({ replacement: 'swift', drawn: false, placement: undefined, textChanged: true })], edited);
		expect(kept.textChanged).toBe(true);
		expect(kept.orphaned).toBe(false);
		const [lost] = commentsInDocumentOrder([tracked({ replacement: 'swift', drawn: false, placement: undefined })], edited);
		expect(lost.textChanged).toBe(false);
		expect(lost.orphaned).toBe(true);
	});

	it('orphans an open suggestion whose whole range was deleted', () => {
		const [row] = commentsInDocumentOrder([tracked({ replacement: 'swift', to: 4 })], 'The  fox');
		expect(row.orphaned).toBe(true);
		expect(row.textChanged).toBe(false);
	});

	it('keeps a card for text deleted from the middle, and orphans one whose whole range was deleted', () => {
		const [middle] = commentsInDocumentOrder([tracked({ to: 7 })], 'The qwn fox');
		expect(middle.orphaned).toBe(false);
		expect(middle.anchor).toBe('qwn');
		const [row] = commentsInDocumentOrder([tracked({ to: 4 })], 'The  fox');
		expect(row.orphaned).toBe(true);
		expect(row.anchor).toBe('quick brown');
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

	it('keeps a point at the position the text was, so its margin card stays beside that line', () => {
		const rows = commentsInDocumentOrder([gone()], text);
		expect(canvasCommentHighlights(rows, text, null, undefined)).toMatchObject([{ id: 'gone', from: 18, to: 18, point: true }]);
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

	it('marks the text its Accept put in the document, like any comment', () => {
		const rows = commentsInDocumentOrder([accepted()], after);
		// The quote stays the quote: the card's diff shows what the Accept changed.
		expect(rows[0]).toMatchObject({ orphaned: false, detached: false, anchor: 'nine' });
		const [range] = canvasCommentHighlights(rows, after, null, undefined);
		expect(range).toMatchObject({ id: 's1', from: 34, to: 37, suggestion: undefined });
		expect(range.point).toBeUndefined();
		expect(canvasCommentScrollTarget(rows, after, 's1', false, 'navigation')).toMatchObject({ from: 34, to: 37 });
	});

	it('sits at a point when its Accept deleted the text', () => {
		const text = 'Lunch is at noon. Train leaves at .';
		const rows = commentsInDocumentOrder([accepted({ replacement: '' })], text);
		expect(rows[0]).toMatchObject({ orphaned: false, detached: false });
		expect(canvasCommentHighlights(rows, text, null, undefined)).toMatchObject([{ id: 's1', from: 34, to: 34, point: true }]);
	});

	it('keeps a decided conversation at its last position without calling it lost', () => {
		const text = 'Lunch is at noon. Train leaves at .';
		const rows = commentsInDocumentOrder([accepted()], text);
		expect(rows[0]).toMatchObject({ orphaned: false, detached: false });
		expect(commentSurfaceStatus(rows)).toEqual({ commentCount: 1 });
		expect(canvasCommentHighlights(rows, text, null, undefined)).toMatchObject([{ id: 's1', point: true }]);
	});

	it('draws nothing once it is resolved', () => {
		const rows = commentsInDocumentOrder([accepted({ resolved: true })], after);
		expect(canvasCommentHighlights(rows, after, null, undefined)).toEqual([]);
	});

	it('is not counted as text that could not be placed', () => {
		const rows = commentsInDocumentOrder([accepted()], after);
		expect(rows[0].orphaned).toBe(false);
		expect(commentSurfaceStatus(rows)).toEqual({ commentCount: 1 });
	});

	it('reads as accepted on its new text while its own Accept is in flight and the replacement already stands where the quote was', () => {
		const open = accepted({ decision: undefined, replies: [] });
		const [row] = commentsInDocumentOrder([open], after, {}, 's1');
		expect(row).toMatchObject({ orphaned: false, decision: 'accepted', anchor: 'nine', from: 34, to: 37 });
		expect(canvasCommentHighlights([row], after, null, undefined, 's1')).toMatchObject([{ from: 34, to: 37, suggestion: undefined }]);
		const [other] = commentsInDocumentOrder([open], after);
		expect(other.orphaned).toBe(true);
	});

	describe('while its own Accept is in flight, before the document holds the edit', () => {
		// The quote is still where it was: only a replacement that is really in the text, in place of the quote, reads as accepted.
		const before = 'Train leaves at very important text today.';
		const open = (anchor: string, replacement: string) => comment('s3', 16, anchor, { replacement, drawn: true });

		it('does not read a deletion as accepted', () => {
			const [row] = commentsInDocumentOrder([open('very important text ', '')], before, {}, 's3');
			expect(row.decision).toBeUndefined();
			expect(canvasCommentHighlights([row], before, null, undefined, 's3')).toMatchObject([{ id: 's3', from: 16, suggestion: { busy: true } }]);
		});

		it('does not read a replacement that is a prefix of its quote as accepted', () => {
			const [row] = commentsInDocumentOrder([open('very important text', 'very important')], before, {}, 's3');
			expect(row.decision).toBeUndefined();
			expect(row).toMatchObject({ from: 16, to: 35 });
		});

		it('reads a deletion as accepted once the text no longer holds the quote', () => {
			const [row] = commentsInDocumentOrder([open('very important text ', '')], 'Train leaves at today.', {}, 's3');
			expect(row.decision).toBe('accepted');
		});

		it('reads a prefix replacement as accepted once the text no longer holds the quote', () => {
			const [row] = commentsInDocumentOrder([open('very important text', 'very important')], 'Train leaves at very important today.', {}, 's3');
			expect(row.decision).toBe('accepted');
			expect(row).toMatchObject({ from: 16, to: 30 });
		});
	});

	describe('an accepted insertion sits on the text it inserted', () => {
		const inserted = 'Train leaves at ten sharp.';
		const insertion = (from: number, overrides: Partial<DocComment> = {}): DocComment =>
			accepted({ id: 's2', from, to: from + 3, anchor: 'ten', replacement: ' sharp', insertAfter: true, ...overrides });

		it('where the server places it, after its quote', () => {
			const [row] = commentsInDocumentOrder([insertion(19)], inserted);
			expect(row).toMatchObject({ orphaned: false, detached: false, anchor: 'ten' });
			expect(canvasCommentHighlights([row], inserted, null, undefined)).toMatchObject([{ id: 's2', from: 19, to: 25, suggestion: undefined }]);
		});

		it('while its own Accept is in flight, from the quote it follows', () => {
			const [row] = commentsInDocumentOrder([insertion(16, { decision: undefined, replies: [] })], inserted, {}, 's2');
			expect(row).toMatchObject({ orphaned: false, decision: 'accepted', from: 19, to: 25 });
		});
	});
});
