import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepAboveSheet, revealAboveSheet, SHEET_SPACE } from './chatCommentViewport';

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
	// Space between the header (60 + 8) and the sheet resting at 852 - 300 (552 - 8): 68..544, centre 306.
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

	it('keeps space at the end of the transcript as tall as the sheet, and gives it back when the sheet is gone', () => {
		const { panel, scroller } = setup(300);
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
		const stop = keepAboveSheet(panel, () => scroller, () => new DOMRect(0, 200, 200, 20));
		frames.splice(0).forEach(callback => callback(0));
		expect(scroller.style.getPropertyValue(SHEET_SPACE)).toBe('300px');
		stop();
		expect(scroller.style.getPropertyValue(SHEET_SPACE)).toBe('');
		vi.unstubAllGlobals();
	});

	it('shows a passage taller than the space from its first line', () => {
		const { panel, scroller, scrollBy } = setup(300);
		revealAboveSheet(scroller, new DOMRect(0, 600, 200, 900), panel, true);
		expect(scrollBy).toHaveBeenCalledWith({ top: 600 - 68 });
	});
});
