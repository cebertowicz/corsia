import { describe, expect, it } from 'vitest';
import { createMentionSelection } from './mentionSelection';

describe('picker selections', () => {
	it('sends only selected tags that survive edits, and follows edits before them', () => {
		const tags = createMentionSelection();
		tags.update('@Alpha ');
		tags.select('agent-a', 0, '@Alpha');
		tags.update('Hi @Alpha ');
		tags.update('Hi @Alpha and @Beta');
		expect(tags.ids('Hi @Alpha and @Beta')).toEqual(['agent-a']);
		tags.update('Hi @Alph and @Beta');
		expect(tags.ids('Hi @Alph and @Beta')).toEqual([]);
	});
	it('deduplicates repeat selections while preserving one surviving selection', () => {
		const tags = createMentionSelection('@A @A');
		tags.select('agent-a', 0, '@A');
		tags.select('agent-a', 3, '@A');
		tags.update('@A');
		expect(tags.ids('@A')).toEqual(['agent-a']);
	});
});
