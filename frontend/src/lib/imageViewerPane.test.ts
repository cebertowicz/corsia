import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { paneCollapsed, paneSnapping } from '$lib/stores/paneCollapsed';
import { paneOpen } from '$lib/stores/paneOpen';
import { WIDE_ENOUGH_LEAF, widenForImageViewer } from './imageViewerPane';

const realWidth = window.innerWidth;
const setWindowWidth = (width: number) => Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });

beforeEach(() => {
	setWindowWidth(1440);
	paneCollapsed.set(false);
	paneOpen.set(false);
});
afterEach(() => setWindowWidth(realWidth));

describe('widenForImageViewer', () => {
	it('collapses the workspace pane for the viewer and hands it back on the way out', () => {
		const release = widenForImageViewer(420);
		expect(get(paneCollapsed)).toBe(true);
		release();
		expect(get(paneCollapsed)).toBe(false);
	});

	it('restores both stores to what they were, because a plan leaf sets both', () => {
		paneOpen.set(true);
		const release = widenForImageViewer(420);
		expect([get(paneCollapsed), get(paneOpen)]).toEqual([true, false]);
		release();
		expect([get(paneCollapsed), get(paneOpen)]).toEqual([false, true]);
	});

	it('leaves a pane the user had already collapsed collapsed', () => {
		paneCollapsed.set(true);
		widenForImageViewer(420)();
		expect(get(paneCollapsed)).toBe(true);
	});

	it('keeps the user\'s choice when they toggle the pane while the viewer is open (W2)', () => {
		const release = widenForImageViewer(420);
		paneCollapsed.set(false);
		paneCollapsed.set(true);
		release();
		expect(get(paneCollapsed)).toBe(true);

		paneCollapsed.set(false);
		const second = widenForImageViewer(420);
		paneCollapsed.set(false);
		second();
		expect(get(paneCollapsed)).toBe(false);
	});

	it('touches nothing when the leaf is already as wide as collapsing makes it (W3)', () => {
		const release = widenForImageViewer(WIDE_ENOUGH_LEAF);
		expect(get(paneCollapsed)).toBe(false);
		release();
		expect(get(paneCollapsed)).toBe(false);
	});

	it('touches nothing at drawer and phone widths (W4)', () => {
		for (const width of [1024, 800, 393]) {
			setWindowWidth(width);
			widenForImageViewer(300)();
			expect([get(paneCollapsed), get(paneOpen)]).toEqual([false, false]);
			const release = widenForImageViewer(300);
			expect(get(paneCollapsed)).toBe(false);
			release();
		}
	});

	it('restores once however many times it is released, and never after a later change', () => {
		const release = widenForImageViewer(420);
		release();
		paneCollapsed.set(true);
		release();
		expect(get(paneCollapsed)).toBe(true);
	});

	it('eases the pane unless told to snap, each way, and lifts a snap once the next frames are laid out', () => {
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
		const nextFrame = () => { for (const callback of frames.splice(0)) callback(0); };
		try {
			const eased = widenForImageViewer(420);
			expect(get(paneSnapping)).toBe(false);
			eased({ instant: true });
			expect([get(paneCollapsed), get(paneSnapping)]).toEqual([false, true]);
			nextFrame();
			expect(get(paneSnapping)).toBe(true);
			nextFrame();
			expect(get(paneSnapping)).toBe(false);

			const snapped = widenForImageViewer(420, { instant: true });
			expect([get(paneCollapsed), get(paneSnapping)]).toEqual([true, true]);
			// Back right after a snapped return still eases, and the pending lift leaves it so.
			snapped();
			expect([get(paneCollapsed), get(paneSnapping)]).toEqual([false, false]);
			nextFrame();
			nextFrame();
			expect(get(paneSnapping)).toBe(false);
		} finally {
			vi.unstubAllGlobals();
		}
	});
});
