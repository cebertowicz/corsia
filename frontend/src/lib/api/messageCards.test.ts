import { describe, expect, it } from 'vitest';
import { feedbackLine } from './messageCards';

describe('feedbackLine', () => {
	it.each([
		[{ comments: 1, suggestions: 1 }, '1 comment, 1 suggestion'],
		[{ comments: 2, suggestions: 0 }, '2 comments'],
		[{ comments: 0, suggestions: 3 }, '3 suggestions'],
	])('formats nonzero counts in %o', (counts, expected) => {
		expect(feedbackLine(counts)).toBe(expected);
	});
});
