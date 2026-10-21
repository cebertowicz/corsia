import { describe, it, expect } from 'vitest';
import { subagentGlyph } from './subagentLabel';

describe('subagentGlyph', () => {
	it('returns ⌘ for cc', () => expect(subagentGlyph('cc')).toBe('⌘'));
	it('returns ⊙ for codex', () => expect(subagentGlyph('codex')).toBe('⊙'));
	it('returns ◈ for native', () => expect(subagentGlyph('native')).toBe('◈'));
	it('returns ◈ for unknown provider', () => expect(subagentGlyph('delegation')).toBe('◈'));
	it('returns ◈ for undefined', () => expect(subagentGlyph(undefined)).toBe('◈'));
});
