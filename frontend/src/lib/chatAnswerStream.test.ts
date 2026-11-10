import { describe, expect, it } from 'vitest';
import cases from '../../../backend/tests/fixtures/chat-answer-tags.json';
import { hasAnswerTag, visibleStreamText } from './chatAnswerStream';

describe('shared answer tag cases', () => {
	it.each(cases)('$name', ({ text, before_first_tag, stream_visible }) => {
		expect(visibleStreamText(text)).toBe(stream_visible);
		expect(hasAnswerTag(text)).toBe(before_first_tag !== null);
		if (before_first_tag !== null) expect(stream_visible).toBe(before_first_tag);
	});
});

describe('an answer while it streams', () => {
	it('never shows a tag or its section at any point of the stream', () => {
		const message = 'Here is the plan.\n\n[c18.1] First answer.\n\n[c18.2] Second answer.';
		for (let end = 0; end <= message.length; end++) {
			const shown = visibleStreamText(message.slice(0, end));
			expect(message.startsWith(shown)).toBe(true);
			expect(shown).not.toContain('[');
			expect(shown).not.toContain('answer');
		}
		expect(visibleStreamText(message)).toBe('Here is the plan.');
	});
});
