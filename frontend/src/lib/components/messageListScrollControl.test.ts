import { describe, expect, it } from 'vitest';
import { MessageListScrollControl } from './messageListScrollControl';
import { SHEET_SPACE } from './chatCommentViewport';

function control(el: HTMLElement) {
	return new MessageListScrollControl(() => el, () => {}, () => {});
}

function scroller(): HTMLElement {
	const el = document.createElement('div');
	Object.defineProperty(el, 'scrollHeight', { value: 2000, configurable: true });
	Object.defineProperty(el, 'clientHeight', { value: 800, configurable: true });
	el.scrollTop = 300;
	return el;
}

describe('restoreBottomOffset', () => {
	it('puts the transcript back against its bottom edge after the keyboard moved it', () => {
		const el = scroller();
		const c = control(el);
		c.captureBottomOffset();
		Object.defineProperty(el, 'scrollTop', { value: 0, writable: true, configurable: true });
		c.restoreBottomOffset();
		expect(el.scrollTop).toBe(300);
	});

	it('leaves the transcript where an open comment sheet put it', () => {
		const el = scroller();
		const c = control(el);
		c.captureBottomOffset();
		Object.defineProperty(el, 'scrollTop', { value: 520, writable: true, configurable: true });
		el.style.setProperty(SHEET_SPACE, '699px');
		c.restoreBottomOffset();
		expect(el.scrollTop).toBe(520);
	});
});
