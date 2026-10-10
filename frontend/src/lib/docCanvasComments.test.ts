import { describe, expect, it } from 'vitest';
import { canvasCommentHighlights, canvasCommentRange, canvasCommentScrollTarget, canvasFocusedCommentScrollTarget, commentSurfaceStatus, commentsInDocumentOrder } from './docCanvasComments';
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
		expect(canvasCommentHighlights([row], text, null, undefined)).toHaveLength(orphaned ? 0 : 1);
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

	it.each([
		['accepted', { decision: 'accepted' as const }],
		['rejected', { decision: 'rejected' as const }],
		['resolved', { resolved: true }],
	])('draws nothing for a %s suggestion', (_name, overrides) => {
		expect(highlights([suggestion('s1', 8, quote, overrides)])).toEqual([]);
	});

	it('draws nothing for a suggestion whose quote no longer matches the text on screen', () => {
		const rows = commentsInDocumentOrder([suggestion('s1', 8, quote)], 'Before. The team changed its mind. After.');
		expect(rows[0].orphaned).toBe(true);
		expect(canvasCommentHighlights(rows, 'Before. The team changed its mind. After.', null, undefined)).toEqual([]);
	});
});

describe('comment surface status', () => {
	it('counts orphaned cards and reports the placement reload state', () => {
		expect(commentSurfaceStatus([])).toEqual({ orphanedCount: 0, placementReloading: false, placementReloadError: false });
		expect(commentSurfaceStatus([
			comment('a', 0, 'x', { orphaned: true }), comment('b', 0, 'x', { placementReloading: true }), comment('c', 0, 'x', { placementReloadError: true }),
		])).toEqual({ orphanedCount: 1, placementReloading: true, placementReloadError: true });
	});
});
