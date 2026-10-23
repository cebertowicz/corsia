import { describe, expect, it, vi } from 'vitest';
import { pinLiveEdge } from './messageListLayout';

function transcript() {
	const el = document.createElement('div');
	Object.defineProperty(el, 'scrollHeight', { value: 900, configurable: true });
	el.innerHTML = '<div data-message-id="opening"></div><div data-message-id="reply"></div>';
	const opening = el.querySelector<HTMLElement>('[data-message-id="opening"]')!;
	opening.scrollIntoView = vi.fn();
	return { el, opening };
}

describe('pinLiveEdge', () => {
	it('lands on the first line of the opening message while it ends the transcript', () => {
		const { el, opening } = transcript();
		pinLiveEdge(el, 'opening');
		expect(opening.scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
		expect(el.scrollTop).toBe(0);
	});

	it('follows the bottom once a later row exists', () => {
		const { el, opening } = transcript();
		pinLiveEdge(el, null);
		expect(opening.scrollIntoView).not.toHaveBeenCalled();
		expect(el.scrollTop).toBe(900);
	});

	it('falls back to the bottom when the opening row is not rendered', () => {
		const { el } = transcript();
		pinLiveEdge(el, 'missing');
		expect(el.scrollTop).toBe(900);
	});
});
