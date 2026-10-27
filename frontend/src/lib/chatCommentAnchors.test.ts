import { describe, expect, it } from 'vitest';
import { clipChatSelection } from './chatCommentAnchors';

describe('chat comment selection', () => {
	it('clips a selection crossing into another message and records UTF-16 offsets', () => {
		const first = document.createElement('div');
		const second = document.createElement('div');
		first.textContent = '😀 first message';
		second.textContent = 'second message';
		document.body.append(first, second);
		const range = document.createRange();
		range.setStart(first.firstChild!, 3);
		range.setEnd(second.firstChild!, 6);
		const selection = window.getSelection()!;
		selection.removeAllRanges();
		selection.addRange(range);
		const result = clipChatSelection(selection, first)!;
		expect(result.anchor).toEqual({ from_off: 3, to_off: 16, quote: 'first message', anchor_prefix: '😀 ', anchor_suffix: '' });
		expect(result.range.toString()).toBe('first message');
		selection.removeAllRanges();
		first.remove(); second.remove();
	});
});
