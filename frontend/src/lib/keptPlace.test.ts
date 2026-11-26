import { describe, expect, it } from 'vitest';
import { findPlace, placeOf, uniquePlaceOf } from './keptPlace';
import windows from './__tests__/fixtures/kept-place-windows.contract.json';

const LEAD = 'An opening paragraph long enough to fill a side.\n\n';

describe('the browser keeps the same sides as the server', () => {
	// backend/tests/test_kept_place.py checks the server's anchor_context against this same file.
	it.each(windows.map((c) => [c.name, c] as const))('%s', (_name, c) => {
		const place = placeOf(c.text, c.from, c.to, c.quote);
		expect([place.prefix, place.suffix]).toEqual([c.prefix, c.suffix]);
	});
});

describe('findPlace places only the one copy with both sides', () => {
	it('finds a passage that moved', () => {
		const text = LEAD + 'The launch moves to Friday.\n';
		const at = text.indexOf('Friday');
		const kept = placeOf(text, at, at + 6);
		const moved = 'A new first line.\n' + text;
		const from = moved.indexOf('Friday');
		expect(findPlace(moved, kept)).toEqual([from, from + 6]);
	});

	it('picks its own copy of a repeated word when the sides differ', () => {
		const text = LEAD + 'Ship on Friday with the team.\nReview on Friday after lunch.\n';
		const second = text.lastIndexOf('Friday');
		expect(findPlace(text, placeOf(text, second, second + 6))).toEqual([second, second + 6]);
	});

	it('places nothing when one side changed', () => {
		const text = LEAD + 'Budget is final for Q3.\n';
		const at = text.indexOf('final');
		const kept = placeOf(text, at, at + 5);
		expect(findPlace(text.replace('for Q3', 'for Q4'), kept)).toBeNull();
		expect(findPlace(text.replace('is final', 'was final'), kept)).toBeNull();
	});

	it('places nothing when two copies have both sides, wherever the kept offset sits', () => {
		const filler = 'ab'.repeat(20);
		const text = filler + 'word' + filler;
		const kept = placeOf(text, filler.length, filler.length + 4);
		const twice = text + 'word' + filler;
		expect(findPlace(twice, kept)).toBeNull();
		expect(findPlace(twice, { ...kept, from: twice.lastIndexOf('word') })).toBeNull();
	});

	it('places nothing for an empty quote', () => {
		expect(findPlace(LEAD, { from: 0, to: 0, quote: '', prefix: '', suffix: LEAD.slice(0, 32) })).toBeNull();
	});
});

describe('a passage keeps its place only when no other copy shares either side', () => {
	const HEAD = LEAD + '| Name | Ok |\n| --- | --- |\n';
	const TAIL = '| End | z |\n';
	/** The comment written on row `own` of six identical rows, then that row deleted: what is kept, and where it lands. */
	function ownRowDeleted(row: string, own: number) {
		const text = HEAD + row.repeat(6) + TAIL;
		const from = HEAD.length + own * row.length + row.indexOf('Yes');
		const kept = uniquePlaceOf(text, from, from + 3);
		const edited = HEAD + row.repeat(5) + TAIL;
		return { kept, placed: kept && findPlace(edited, kept) };
	}

	it.each([0, 1, 2, 3, 4, 5])('short rows: row %i deleted shows unplaced, never on a neighbouring row', (own) => {
		expect(ownRowDeleted('| Yes |\n', own).placed).toBeNull();
	});

	it.each([0, 5])('long rows: edge row %i deleted shows unplaced, never on a neighbouring row', (own) => {
		expect(ownRowDeleted('| Alpha task | Yes | Owner name here |\n', own).placed).toBeNull();
	});

	it('a unique passage is kept and placed where it moved', () => {
		const text = LEAD + 'Ship on Friday with the team.\nReview on Friday after lunch.\n';
		const at = text.lastIndexOf('Friday');
		const kept = uniquePlaceOf(text, at, at + 6);
		expect(kept).toEqual(placeOf(text, at, at + 6));
		const moved = 'A new first line.\n' + text;
		const from = moved.lastIndexOf('Friday');
		expect(kept && findPlace(moved, kept)).toEqual([from, from + 6]);
	});

	it('keeps no place when the given quote is not the text at the passage', () => {
		const text = LEAD + 'The launch moves to Friday.\n';
		const at = text.indexOf('Friday');
		expect(uniquePlaceOf(text, at, at + 6, 'Monday')).toBeNull();
	});
});
