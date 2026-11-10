import { describe, expect, it, vi } from 'vitest';
import { ChangeSet, EditorState, type ChangeSpec } from '@codemirror/state';
import { mapCommentRange, type MappedRange } from './comment-range';
import { commentRanges, setCommentRanges, type CommentRange } from './comment-marks';

vi.mock('$lib/documentReadingPlace', () => ({ releasePlace: vi.fn() }));

// "one quote two": the comment is on "quote", 4..9.
const text = 'one quote two';
const mapped = (from: number, to: number, ...specs: ChangeSpec[]) => {
	// Several specs apply one after another, the way a delete and an insert can arrive as two steps of one edit.
	let changes = ChangeSet.empty(text.length);
	let length = text.length;
	for (const spec of specs) {
		const step = ChangeSet.of(spec, length);
		changes = changes.compose(step);
		length = step.newLength;
	}
	return mapCommentRange({ from, to }, changes);
};
// Each spec its own edit, the way a person types one key at a time.
const typed = (from: number, to: number, ...specs: ChangeSpec[]) => {
	let range: MappedRange = { from, to, open: false };
	let length = text.length;
	for (const spec of specs) {
		const step = ChangeSet.of(spec, length);
		range = mapCommentRange(range, step);
		length = step.newLength;
	}
	return range;
};
// The highlight selected and "Handoff" typed over it a key at a time: the first key replaces it.
const typeOver: ChangeSpec[] = [{ from: 4, to: 9, insert: 'H' }, ...[...'andoff'].map((ch, i) => ({ from: 5 + i, insert: ch }))];

describe('mapCommentRange', () => {
	it('keeps text typed at either edge outside the range', () => {
		expect(mapped(4, 9, { from: 4, insert: 'AB' })).toEqual({ from: 6, to: 11, open: false });
		expect(mapped(4, 9, { from: 9, insert: 'AB' })).toEqual({ from: 4, to: 9, open: false });
		expect(typed(4, 9, { from: 9, insert: 'A' }, { from: 10, insert: 'B' })).toEqual({ from: 4, to: 9, open: false });
	});

	it('moves the range onto text typed over all of it, in one change or as delete then insert', () => {
		expect(mapped(4, 9, { from: 4, to: 9, insert: 'words!' })).toEqual({ from: 4, to: 10, open: true });
		expect(mapped(4, 9, { from: 4, to: 9 }, { from: 4, insert: 'words!' })).toEqual({ from: 4, to: 10, open: true });
	});

	it('keeps the rest of a word typed over the range a key at a time', () => {
		expect(typed(4, 9, ...typeOver)).toEqual({ from: 4, to: 11, open: true });
	});

	it('closes the end at the first edit elsewhere, so typing at it afterwards lands outside', () => {
		expect(typed(4, 9, ...typeOver, { from: 0, insert: 'X' }, { from: 12, insert: '!' })).toEqual({ from: 5, to: 12, open: false });
		expect(typed(4, 9, { from: 4, to: 9, insert: 'H' }, { from: 4, insert: '>' }, { from: 6, insert: 'a' })).toEqual({ from: 5, to: 6, open: false });
	});

	it('leaves an empty range when the text is deleted with nothing in its place', () => {
		expect(mapped(4, 9, { from: 4, to: 9 })).toEqual({ from: 4, to: 4, open: false });
	});

	it('keeps what is left of the text after a partial edit', () => {
		expect(mapped(4, 9, { from: 2, to: 6, insert: 'X' })).toEqual({ from: 3, to: 6, open: false });
	});

	it('keeps a point a point, before text typed at it', () => {
		expect(mapped(4, 4, { from: 4, insert: 'AB' })).toEqual({ from: 4, to: 4, open: false });
	});
});

describe('the highlight follows the same rule', () => {
	const after = (changes: ChangeSpec[], range: Partial<CommentRange> = {}) => {
		let state = EditorState.create({ doc: text, extensions: [commentRanges] })
			.update({ effects: setCommentRanges.of([{ id: 'c1', from: 4, to: 9, ...range }]) }).state;
		for (const change of changes) state = state.update({ changes: change }).state;
		return state.field(commentRanges);
	};

	it('stays on text typed over the whole highlight', () => {
		expect(after([{ from: 4, to: 9, insert: 'words!' }])).toEqual([expect.objectContaining({ id: 'c1', from: 4, to: 10 })]);
	});

	it('covers a word typed over it a key at a time, until an edit elsewhere', () => {
		expect(after(typeOver)).toEqual([expect.objectContaining({ from: 4, to: 11 })]);
		expect(after([...typeOver, { from: 0, insert: 'X' }, { from: 12, insert: '!' }])).toEqual([expect.objectContaining({ from: 5, to: 12 })]);
	});

	it('keeps the open end when the comments are set again unchanged', () => {
		let state = EditorState.create({ doc: text, extensions: [commentRanges] })
			.update({ effects: setCommentRanges.of([{ id: 'c1', from: 4, to: 9 }]) }).state;
		state = state.update({ changes: { from: 4, to: 9, insert: 'H' } }).state;
		state = state.update({ effects: setCommentRanges.of([{ id: 'c1', from: 4, to: 5 }]) }).state;
		state = state.update({ changes: { from: 5, insert: 'a' } }).state;
		expect(state.field(commentRanges)).toEqual([expect.objectContaining({ from: 4, to: 6 })]);
	});

	it('drops the highlight whose text is deleted', () => {
		expect(after([{ from: 4, to: 9 }])).toEqual([]);
	});

	it('keeps typing at its first and last character outside it', () => {
		expect(after([{ from: 4, insert: 'A' }])).toEqual([expect.objectContaining({ from: 5, to: 10 })]);
		expect(after([{ from: 9, insert: 'A' }])).toEqual([expect.objectContaining({ from: 4, to: 9 })]);
	});

	it('ends a suggestion whose quoted text is typed over', () => {
		expect(after([{ from: 4, to: 9, insert: 'words!' }], { suggestion: { quote: 'quote', insert: 'x', color: '#000', inline: true } })).toEqual([]);
	});
});
