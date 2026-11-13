import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepAboveSheet, revealAboveSheet } from './chatCommentViewport';

function setup(sheetHeight: number) {
	document.body.innerHTML = '<header class="shell-header"></header><div class="messages"></div><div data-adaptive-drawer><div class="drawer-panel"></div></div>';
	const header = document.querySelector<HTMLElement>('.shell-header')!;
	const backdrop = document.querySelector<HTMLElement>('[data-adaptive-drawer]')!;
	const panel = document.querySelector<HTMLElement>('.drawer-panel')!;
	const scroller = document.querySelector<HTMLElement>('.messages')!;
	vi.spyOn(header, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 60));
	vi.spyOn(backdrop, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 852));
	Object.defineProperty(panel, 'offsetHeight', { value: sheetHeight });
	const scrollBy = vi.fn();
	scroller.scrollBy = scrollBy as typeof scroller.scrollBy;
	return { panel, scroller, scrollBy };
}

afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = ''; });

describe('revealAboveSheet', () => {
	// Space between the header (60 + 16) and the sheet resting at 852 - 300 (552 - 16): 76..536, centre 306.
	it('brings a passage hidden under the header into the space above the sheet, centred', () => {
		const { panel, scroller, scrollBy } = setup(300);
		revealAboveSheet(scroller, new DOMRect(0, 20, 200, 20), panel, true);
		expect(scrollBy).toHaveBeenCalledWith({ top: 30 - 306 });
	});

	it('leaves a passage that is already in view where it is', () => {
		const { panel, scroller, scrollBy } = setup(300);
		revealAboveSheet(scroller, new DOMRect(0, 200, 200, 20), panel, true);
		expect(scrollBy).not.toHaveBeenCalled();
	});

	it('lifts a passage the sheet covers once it moves, but not one the reader scrolled above', () => {
		const { panel, scroller, scrollBy } = setup(300);
		revealAboveSheet(scroller, new DOMRect(0, 20, 200, 20), panel, false);
		expect(scrollBy).not.toHaveBeenCalled();
		revealAboveSheet(scroller, new DOMRect(0, 600, 200, 20), panel, false);
		expect(scrollBy).toHaveBeenCalledWith({ top: 610 - 306 });
	});

	it('adds room below the transcript when the passage cannot rise without it, and gives it back with the stop', () => {
		const { panel, scroller, scrollBy } = setup(300);
		Object.defineProperty(scroller, 'scrollHeight', { value: 1000 });
		Object.defineProperty(scroller, 'clientHeight', { value: 700 });
		scroller.scrollTop = 290;
		scroller.style.paddingBottom = '4px';
		const stop = keepAboveSheet(panel, () => scroller, () => new DOMRect(0, 600, 200, 20));
		revealAboveSheet(scroller, new DOMRect(0, 600, 200, 20), panel, true);
		// 1000 - 700 - 290 = 10 left; the passage needs 610 - 306 = 304: 294 more.
		expect(scroller.style.paddingBottom).toBe('298px');
		stop();
		expect(scroller.style.paddingBottom).toBe('4px');
	});

	it('shows a passage taller than the space from its first line', () => {
		const { panel, scroller, scrollBy } = setup(300);
		revealAboveSheet(scroller, new DOMRect(0, 600, 200, 900), panel, true);
		expect(scrollBy).toHaveBeenCalledWith({ top: 600 - 76 });
	});
});
