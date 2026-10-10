import { describe, expect, it } from 'vitest';
import { prettifyCCModel } from '../ccTakeover';

describe('prettifyCCModel', () => {
	it('formats a resolved id into a tier label', () => {
		expect(prettifyCCModel('claude-opus-4-8')).toBe('Opus 4.8');
		expect(prettifyCCModel('claude-fable-5')).toBe('Fable 5');
		expect(prettifyCCModel('claude-sonnet-4-6')).toBe('Sonnet 4.6');
	});

	it('strips the [1m] long-context marker', () => {
		expect(prettifyCCModel('claude-opus-4-8[1m]')).toBe('Opus 4.8');
	});

	it('falls back to "Claude Code" on empty / unparseable input', () => {
		expect(prettifyCCModel(null)).toBe('Claude Code');
		expect(prettifyCCModel(undefined)).toBe('Claude Code');
		expect(prettifyCCModel('')).toBe('Claude Code');
	});
});
