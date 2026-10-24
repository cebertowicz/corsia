import { describe, expect, it, vi } from 'vitest';
import { landOnFirstRunOpening } from './messageListLayout';

function transcript() {
	const el = document.createElement('div');
	el.innerHTML = '<div data-message-id="opening" data-first-run-opening></div><div data-message-id="reply"></div>';
	const opening = el.querySelector<HTMLElement>('[data-message-id="opening"]')!;
	opening.scrollIntoView = vi.fn();
	return { el, opening };
}

describe('landOnFirstRunOpening', () => {
	it('lands on the first line of the designated opening message', () => {
		const { el, opening } = transcript();
		landOnFirstRunOpening(el);
		expect(opening.scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
	});

	it('does not invent a landing target when the opening row is absent', () => {
		const { el } = transcript();
		el.querySelector('[data-first-run-opening]')?.remove();
		expect(() => landOnFirstRunOpening(el)).not.toThrow();
	});
});
