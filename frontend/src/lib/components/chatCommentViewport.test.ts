import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepAboveSheet } from './chatCommentViewport';
import { liftSheetCap } from '$lib/sheetClearance';
import { createSheetDrag } from '$lib/sheetDrag.svelte';

function setup(sheetHeight: number) {
	document.body.innerHTML = '<header class="shell-header"></header><div class="messages"></div><div data-adaptive-drawer><div class="drawer-panel"></div></div>';
	const header = document.querySelector<HTMLElement>('.shell-header')!;
	const backdrop = document.querySelector<HTMLElement>('[data-adaptive-drawer]')!;
	const panel = document.querySelector<HTMLElement>('.drawer-panel')!;
	const scroller = document.querySelector<HTMLElement>('.messages')!;
	vi.spyOn(header, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 60));
	vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 852));
	vi.spyOn(backdrop, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 393, 852));
	Object.defineProperty(panel, 'offsetHeight', { value: sheetHeight, configurable: true });
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

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); document.body.innerHTML = ''; });

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

	it('once the reader pulls a capped sheet up, neither its growth nor the keyboard after it moves the transcript or caps it again', () => {
		const { panel, scroller } = setup(300);
		const resized: ResizeObserverCallback[] = [];
		vi.stubGlobal('ResizeObserver', class { constructor(callback: ResizeObserverCallback) { resized.push(callback); } observe() {} disconnect() {} });
		const viewport = new EventTarget();
		Object.defineProperty(window, 'visualViewport', { value: viewport, configurable: true });
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
		vi.stubGlobal('cancelAnimationFrame', () => {});
		const flush = () => frames.splice(0).forEach(callback => callback(0));
		const pressed = new DOMRect(0, 600, 200, 300);
		const stop = keepAboveSheet(panel, () => scroller, () => pressed, () => pressed);
		flush();
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('460px');
		const placed = scroller.scrollTop;
		expect(placed).not.toBe(500);
		liftSheetCap(panel);
		resized.forEach(callback => callback([], {} as ResizeObserver));
		flush();
		viewport.dispatchEvent(new Event('resize'));
		flush();
		expect(scroller.scrollTop).toBe(placed);
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('');
		stop();
		delete (window as { visualViewport?: unknown }).visualViewport;
	});

	it('moves nothing while the reader pulls the grabber up, step by step, nor once a short pull settles back', () => {
		const { panel, scroller } = setup(300);
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		const resized: ResizeObserverCallback[] = [];
		vi.stubGlobal('ResizeObserver', class { constructor(callback: ResizeObserverCallback) { resized.push(callback); } observe() {} disconnect() {} });
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
		vi.stubGlobal('cancelAnimationFrame', () => {});
		const flush = () => frames.splice(0).forEach(callback => callback(0));
		scroller.style.paddingBottom = '10px';
		// The pressed text sits clear above the resting sheet (top 552), so opening moves nothing.
		const pressed = new DOMRect(0, 400, 200, 40);
		const stop = keepAboveSheet(panel, () => scroller, () => pressed, () => pressed);
		flush();
		const cap = panel.style.getPropertyValue('--drawer-text-cap');
		expect(cap).toBe(`${852 - (60 + 16 + 40 + 16)}px`);
		const still = () => {
			expect(scroller.scrollTop).toBe(500);
			expect(scroller.style.paddingBottom).toBe('10px');
			expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe(cap);
		};
		still();
		const grow = (height: number) => {
			Object.defineProperty(panel, 'offsetHeight', { value: height, configurable: true });
			resized.forEach(callback => callback([], {} as ResizeObserver));
			flush();
		};
		const drag = createSheetDrag({ expandable: () => true, panel: () => panel, onExpand: () => liftSheetCap(panel), onDismiss: vi.fn() });
		const handle = document.createElement('div');
		const at = (type: string, clientY: number) => ({ type, clientY, pointerId: 4, button: 0, currentTarget: handle }) as unknown as PointerEvent;
		drag.start(at('pointerdown', 600));
		// Each 10 px step grows the sheet before any lift; by the third its top (852 - 480) is over the text's bottom (440).
		for (let step = 1; step <= 3; step++) {
			drag.move(at('pointermove', 600 - step * 10));
			grow(300 + step * 60);
			still();
		}
		drag.finish(at('pointerup', 570));
		grow(300);
		still();
		vi.advanceTimersByTime(300);
		flush();
		still();
		stop();
	});
});
