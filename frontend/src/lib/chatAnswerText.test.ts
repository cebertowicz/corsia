import { describe, expect, it } from 'vitest';
import { visibleChatAnswerText } from './chatAnswerText';

describe('placed chat answer text', () => {
	it('hides one answer shared by two comments while keeping an ordinary introduction', () => {
		expect(visibleChatAnswerText('Introduction\n\n[c1.1][c1.2] shared answer', ['c1.1', 'c1.2'])).toBe('Introduction');
	});
	it('leaves an unplaced citation visible and removes only placed sections', () => {
		expect(visibleChatAnswerText('[c1.9] unknown\n\n[c1.1] placed', ['c1.1'])).toBe('[c1.9] unknown');
	});
	it('keeps the leading post and places a trailing paragraph with the final answer', () => {
		expect(visibleChatAnswerText('Leading post\n\n[c1.1] answer\n\nTrailing paragraph', ['c1.1'])).toBe('Leading post');
	});
	it('removes a whole answer-only post', () => {
		expect(visibleChatAnswerText('[c1.1] answer', ['c1.1'])).toBe('');
	});
});
