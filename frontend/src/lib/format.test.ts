import { humanTokens } from './format';

describe('humanTokens', () => {
	it('passes small counts through', () => {
		expect(humanTokens(0)).toBe('0');
		expect(humanTokens(850)).toBe('850');
	});

	it('formats thousands with one decimal under 10k', () => {
		expect(humanTokens(1200)).toBe('1.2k');
		expect(humanTokens(9900)).toBe('9.9k');
	});

	it('rounds whole-k from 10k up', () => {
		expect(humanTokens(12_400)).toBe('12k');
		expect(humanTokens(204_800)).toBe('205k');
	});

	it('formats millions', () => {
		expect(humanTokens(1_000_000)).toBe('1M');
		expect(humanTokens(1_500_000)).toBe('1.5M');
	});
});

describe('humanTokens billions', () => {
	it('reads billions in B', () => {
		expect(humanTokens(1_000_000_000)).toBe('1B');
		expect(humanTokens(7_440_000_000)).toBe('7.4B');
		expect(humanTokens(312_000_000_000)).toBe('312B');
		expect(humanTokens(937_500_000)).toBe('937.5M');
	});
});

describe('humanTokens with significant digits', () => {
	it('keeps the digits asked for and trims trailing zeros', () => {
		expect(humanTokens(7_440_000_000, 4)).toBe('7.44B');
		expect(humanTokens(34_590_000_000, 4)).toBe('34.59B');
		expect(humanTokens(1_500_000, 4)).toBe('1.5M');
		expect(humanTokens(2_400_000, 4)).toBe('2.4M');
		expect(humanTokens(1_000_000_000, 4)).toBe('1B');
		expect(humanTokens(142_000, 4)).toBe('142k');
		expect(humanTokens(1_234_567, 4)).toBe('1.235M');
		expect(humanTokens(312_480_000_000, 4)).toBe('312.5B');
	});

	it('passes counts under a thousand through and carries a rounding into the next unit', () => {
		expect(humanTokens(0, 4)).toBe('0');
		expect(humanTokens(850, 4)).toBe('850');
		expect(humanTokens(1234, 4)).toBe('1.234k');
		expect(humanTokens(999_999, 4)).toBe('1M');
		expect(humanTokens(999_999_999, 4)).toBe('1B');
	});

	it('leaves the default output as it was when no digits are given', () => {
		expect(humanTokens(7_440_000_000)).toBe('7.4B');
		expect(humanTokens(34_590_000_000)).toBe('34.6B');
		expect(humanTokens(142_000)).toBe('142k');
		expect(humanTokens(999_999)).toBe('1000k');
	});
});
