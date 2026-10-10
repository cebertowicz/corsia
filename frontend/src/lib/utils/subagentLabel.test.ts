import { describe, it, expect, vi } from 'vitest';

// Mock ccTakeover to isolate our functions
vi.mock('./ccTakeover', () => ({
	prettifyCCModel: (model: string | null | undefined) =>
		model ? `CC:${model.split('/').pop()}` : 'Claude Code',
}));

import { subagentGlyph, subagentModelLabel, subagentTitle } from './subagentLabel';

describe('subagentGlyph', () => {
	it('returns ⌘ for cc', () => expect(subagentGlyph('cc')).toBe('⌘'));
	it('returns ⊙ for codex', () => expect(subagentGlyph('codex')).toBe('⊙'));
	it('returns ◈ for native', () => expect(subagentGlyph('native')).toBe('◈'));
	it('returns ◈ for unknown provider', () => expect(subagentGlyph('delegation')).toBe('◈'));
	it('returns ◈ for undefined', () => expect(subagentGlyph(undefined)).toBe('◈'));
});

describe('subagentModelLabel', () => {
	it('cc: delegates to prettifyCCModel with model', () => {
		expect(subagentModelLabel('claude-code/sonnet', 'cc')).toBe('CC:sonnet');
	});
	it('cc: delegates to prettifyCCModel with undefined model', () => {
		expect(subagentModelLabel(undefined, 'cc')).toBe('Claude Code');
	});
	it('codex: no model → "Codex"', () => {
		expect(subagentModelLabel(null, 'codex')).toBe('Codex');
	});
	it('native: no model → "Native"', () => {
		expect(subagentModelLabel(null, 'native')).toBe('Native');
	});
	it('native: unknown provider no model → "Native"', () => {
		expect(subagentModelLabel(undefined, undefined)).toBe('Native');
	});
	it('native: shows last path segment for omlx model', () => {
		expect(subagentModelLabel('omlx@server/Qwen-35B', 'native')).toBe('Qwen-35B');
	});
	it('native: shows last path segment for anthropic model', () => {
		expect(subagentModelLabel('anthropic/claude-3-5-sonnet', 'native')).toBe('claude-3-5-sonnet');
	});
	it('truncates labels longer than 20 chars', () => {
		const label = subagentModelLabel('native/very-long-model-name-here', 'native');
		expect(label.length).toBeLessThanOrEqual(21); // 18 + '…'
		expect(label.endsWith('…')).toBe(true);
	});
	it('does not truncate labels of exactly 20 chars', () => {
		// 20-char model segment — no truncation
		const model = 'native/' + 'A'.repeat(20);
		const label = subagentModelLabel(model, 'native');
		expect(label).toBe('A'.repeat(20));
	});
});

describe('subagentTitle', () => {
	it('cc → Claude Code session model', () => {
		expect(subagentTitle('cc')).toBe('Claude Code session model');
	});
	it('codex → Codex session model', () => {
		expect(subagentTitle('codex')).toBe('Codex session model');
	});
	it('native → Native thread model', () => {
		expect(subagentTitle('native')).toBe('Native thread model');
	});
	it('undefined → Native thread model', () => {
		expect(subagentTitle(undefined)).toBe('Native thread model');
	});
});
