import { describe, expect, it } from 'vitest';
import { fitsOneRow, shouldStack } from './headerStack';

// What the review line needs in the first column: avatar, name at its floor, a 16-character ask, the chevron and the gaps between them.
const floor = 19 + 8 + 56 + 8 + 122 + 8 + 13;

describe('shouldStack', () => {
	const tight = { content: 636, others: 440, gaps: 8, floor };

	it('stacks a tight header on a tall viewport', () => {
		expect(shouldStack(tight, false)).toBe(true);
	});

	it('keeps the same tight header on one row on a short viewport', () => {
		expect(shouldStack(tight, true)).toBe(false);
	});

	it('still stacks a header narrower than 408px on a short viewport', () => {
		expect(shouldStack({ ...tight, content: 407 }, true)).toBe(true);
		expect(shouldStack({ ...tight, content: 408 }, true)).toBe(false);
	});
});

describe('fitsOneRow', () => {
	it('holds the line beside the tools when the first column keeps its floors', () => {
		expect(fitsOneRow({ content: 900, others: 440, gaps: 8, floor })).toBe(true);
	});

	it('stacks when the tools as drawn leave the line less than its floors', () => {
		expect(fitsOneRow({ content: 636, others: 440, gaps: 8, floor })).toBe(false);
	});

	it('stacks a narrow phone header whose tools are small, with a name that is not cut', () => {
		const wholeNameFloor = 19 + 8 + 129 + 8 + 122 + 8 + 13;
		expect(fitsOneRow({ content: 345, others: 94, gaps: 8, floor: wholeNameFloor })).toBe(false);
	});

	it('fits exactly at the floor and not one pixel under', () => {
		expect(fitsOneRow({ content: floor + 440 + 8, others: 440, gaps: 8, floor })).toBe(true);
		expect(fitsOneRow({ content: floor + 440 + 8 - 1, others: 440, gaps: 8, floor })).toBe(false);
	});

	it('counts the presence row with the tools', () => {
		expect(fitsOneRow({ content: 700, others: 500, gaps: 8, floor })).toBe(false);
		expect(fitsOneRow({ content: 700, others: 440, gaps: 8, floor })).toBe(true);
	});
});
