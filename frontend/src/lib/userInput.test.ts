import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { lastUserInputAt } from './userInput';

describe('the page input clock', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(5_000_000);
	});

	afterEach(() => {
		document.body.innerHTML = '';
		vi.useRealTimers();
	});

	it('does not count focus the app moves to an element', () => {
		const before = lastUserInputAt();
		document.body.innerHTML = '<div tabindex="-1" data-drawer-autofocus>Plan</div>';
		vi.advanceTimersByTime(60_000);

		(document.querySelector('[data-drawer-autofocus]') as HTMLElement).focus();

		expect(lastUserInputAt()).toBe(before);
	});

	it('counts the window gaining focus, the user coming back to the tab', () => {
		vi.advanceTimersByTime(60_000);

		window.dispatchEvent(new FocusEvent('focus'));

		expect(lastUserInputAt()).toBe(Date.now());
	});
});
