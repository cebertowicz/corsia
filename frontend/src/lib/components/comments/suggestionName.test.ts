import { describe, expect, it } from 'vitest';
import { suggestionName } from './suggestionName';

describe('suggestionName', () => {
	it('names a replacement by what it changes', () => {
		expect(suggestionName('Step 1', 'Phase 1')).toBe('Step 1 to Phase 1');
	});

	it('names a deletion and an insertion', () => {
		expect(suggestionName('the next ring', '')).toBe('delete the next ring');
		expect(suggestionName('Step 1', 'a new line', true)).toBe('insert a new line');
	});

	it('collapses whitespace and cuts a long text with an ellipsis', () => {
		expect(suggestionName('one\n  two', 'three')).toBe('one two to three');
		const long = 'word '.repeat(30);
		const name = suggestionName(long, 'short');
		expect(name.startsWith('word word')).toBe(true);
		expect(name).toContain('…');
		expect(name.length).toBeLessThan(60);
	});

	it('still names a suggestion with no text to quote', () => {
		expect(suggestionName('', '')).toBe('a deletion');
		expect(suggestionName('', '', true)).toBe('an insertion');
	});
});
