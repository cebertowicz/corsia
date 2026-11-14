import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepAboveSheet } from './chatCommentViewport';

function setup(sheetHeight: number) {
	document.body.innerHTML = '<header class="shell-header"></header><div class="messages"></div><div data-adaptive-drawer><div class="drawer-panel"></div></div>';
	const header = document.querySelector<HTMLElement>('.shell-header')!;
	const backdrop = document.querySelector<HTMLElement>('[data-adaptive-drawer]')!;
	const panel = document.querySelector<HTMLElement>('.drawer-panel')!;
	const scroller = document.querySelector<HTMLElement>('.messages')!;
	vi.spyOn(header, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 60));
	vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 852));
	vi.spyOn(backdrop, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 852));
	Object.defineProperty(panel, 'offsetHeight', { value: sheetHeight });
	Object.defineProperty(scroller, 'scrollHeight', { value: 2000, configurable: true });
	Object.defineProperty(scroller, 'clientHeight', { value: 852, configurable: true });
	scroller.scrollTop = 500;
	return { panel, scroller };
}

function run(panel: HTMLElement, scroller: HTMLElement, passage: DOMRect | null, capText?: () => DOMRect | null) {
	const frames: FrameRequestCallback[] = [];
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
	const stop = keepAboveSheet(panel, () => scroller, () => passage, capText);
	frames.splice(0).forEach(callback => callback(0));
	return stop;
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('keepAboveSheet', () => {
	// Space between the header (60 + 16) and the sheet resting at 852 - 300 (552 - 16): 76..536, centre 306.
	it('brings a passage the sheet covers into the space above it, centred', () => {
		const { panel, scroller } = setup(300);
		run(panel, scroller, new DOMRect(0, 600, 200, 20));
		expect(scroller.scrollTop).toBe(500 + 610 - 306);
	});

	it('leaves a passage that is already in the space where it is', () => {
		const { panel, scroller } = setup(300);
		run(panel, scroller, new DOMRect(0, 200, 200, 20));
		expect(scroller.scrollTop).toBe(500);
	});

	it('caps the sheet to leave the pressed text room above it, only when asked to', () => {
		const { panel, scroller } = setup(300);
		run(panel, scroller, new DOMRect(0, 200, 200, 20));
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('');
		run(panel, scroller, new DOMRect(0, 200, 200, 20), () => new DOMRect(0, 200, 200, 20));
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe(`${852 - (60 + 16 + 20 + 16)}px`);
	});

	it('gives back the room it added below the transcript when the sheet is gone', () => {
		const { panel, scroller } = setup(300);
		Object.defineProperty(scroller, 'scrollHeight', { value: 1400, configurable: true });
		scroller.style.paddingBottom = '10px';
		const stop = run(panel, scroller, new DOMRect(0, 800, 200, 20));
		expect(scroller.style.paddingBottom).not.toBe('10px');
		stop();
		expect(scroller.style.paddingBottom).toBe('10px');
	});
});
