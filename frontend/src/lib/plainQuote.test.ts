import { expect, it } from 'vitest';
import { plainQuote } from './plainQuote';

it('keeps the text of inline marks and drops the marks', () => {
	expect(plainQuote('**Bravo**')).toBe('Bravo');
	expect(plainQuote('Another **bold** word and *italic* and _more_')).toBe('Another bold word and italic and more');
	expect(plainQuote('`code` cell')).toBe('code cell');
	expect(plainQuote('See [the spec](https://example.com/spec)')).toBe('See the spec');
	expect(plainQuote('![diagram](a.png) and ~~old~~')).toBe('diagram and old');
	expect(plainQuote('a \\| b')).toBe('a | b');
	expect(plainQuote('  **Bravo** \n')).toBe('Bravo');
});

it('leaves text that only looks like a mark', () => {
	expect(plainQuote('2 * 3 * 4')).toBe('2 * 3 * 4');
	expect(plainQuote('snake_case_name')).toBe('snake_case_name');
	expect(plainQuote('plain words')).toBe('plain words');
});

it('keeps a code span as written, dropping only its backticks', () => {
	expect(plainQuote('`2*3*4`')).toBe('2*3*4');
	expect(plainQuote('run `a\\.b` and `__init__`')).toBe('run a\\.b and __init__');
	expect(plainQuote('**bold `x*y*z` text**')).toBe('bold x*y*z text');
	expect(plainQuote('``a ` b``')).toBe('a ` b');
});
