import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { paneCollapsed, paneSnapping } from '$lib/stores/paneCollapsed';
import { paneOpen } from '$lib/stores/paneOpen';
import { paneOverlay } from '$lib/stores/artifactCanvas';
import { splitWidth } from '$lib/stores/artifactWorkspace';
import { sidebarCollapsed } from '$lib/stores/ui';
import { HISTORY_BESIDE_CANVAS, WIDE_ENOUGH_LEAF, canvasWithPanesHidden, contentWidth, widenForHistory, widenForImageViewer } from './imageViewerPane';

const realWidth = window.innerWidth;
const setWindowWidth = (width: number) => Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });

beforeEach(() => {
	setWindowWidth(1440);
	paneCollapsed.set(false);
	paneOpen.set(false);
	sidebarCollapsed.set(false);
	paneOverlay.set(false);
	splitWidth.set(null);
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

describe('widenForHistory (version history prototype)', () => {
	it('hides the sidebar and the Workspace panel when that brings the canvas to the threshold, and hands both back', () => {
		setWindowWidth(1920);
		paneOpen.set(true);
		const release = widenForHistory(380, 40);
		expect([get(paneCollapsed), get(paneOpen), get(sidebarCollapsed)]).toEqual([true, false, true]);
		release();
		expect([get(paneCollapsed), get(paneOpen), get(sidebarCollapsed)]).toEqual([false, true, false]);
	});

	it('keeps what the person had already hidden hidden', () => {
		setWindowWidth(1920);
		sidebarCollapsed.set(true);
		paneCollapsed.set(true);
		widenForHistory(600, 40)();
		expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([true, true]);
	});

	it('hides both at 1440 and 1200, where the canvas measured 824 and 680 with both hidden, and takes the new widths at once', () => {
		for (const [width, hidden] of [[1440, 824], [1200, 680]]) {
			setWindowWidth(width);
			expect(canvasWithPanesHidden(width, 40)).toBeCloseTo(hidden, 5);
			const release = widenForHistory(380, 40);
			expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([true, true]);
			expect(get(paneSnapping)).toBe(true);
			release();
			paneCollapsed.set(false);
			sidebarCollapsed.set(false);
		}
	});

	it('touches nothing at 1176, where hiding both still leaves the canvas under the threshold (the strip stays)', () => {
		setWindowWidth(1176);
		expect(canvasWithPanesHidden(1176, 40)).toBeLessThan(HISTORY_BESIDE_CANVAS);
		const release = widenForHistory(380, 40);
		expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([false, false]);
		release();
	});

	it('at 1160, where the sheet stays, closes a Workspace panel the shell shows as an overlay (it would cover the rows) and hands it back; the sidebar stays', () => {
		setWindowWidth(1160);
		paneOverlay.set(true);
		const release = widenForHistory(700, 40);
		expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([true, false]);
		release();
		expect(get(paneCollapsed)).toBe(false);
	});

	it('closes an overlaid panel even where the canvas is already at the threshold', () => {
		setWindowWidth(1300);
		paneOverlay.set(true);
		widenForHistory(HISTORY_BESIDE_CANVAS + 10, 40);
		expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([true, false]);
	});

	it('leaves the panes alone where the shell says the panel takes layout instead of covering the canvas', () => {
		setWindowWidth(1160);
		const release = widenForHistory(700, 40);
		expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([false, false]);
		release();
	});

	it('asks the split itself, so the width the person dragged the divider to is the width both hidden gives', () => {
		setWindowWidth(1920);
		splitWidth.set(500);
		expect(canvasWithPanesHidden(1920, 40)).toBe(460);
		const untouched = widenForHistory(380, 40);
		expect([get(paneCollapsed), get(paneOpen), get(sidebarCollapsed)]).toEqual([false, false, false]);
		untouched();
		splitWidth.set(1000);
		expect(canvasWithPanesHidden(1176, 40)).toBe(708); // the split's own chat minimum caps a wide drag at 748
		const release = widenForHistory(380, 40);
		expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([true, true]);
		release();
		splitWidth.set(null);
		expect(canvasWithPanesHidden(1920, 40)).toBe(1112);
	});

	it('touches nothing when the canvas is already at the threshold, or at drawer and phone widths', () => {
		setWindowWidth(1920);
		widenForHistory(HISTORY_BESIDE_CANVAS, 40)();
		expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([false, false]);
		for (const width of [1024, 393]) {
			setWindowWidth(width);
			widenForHistory(300, 40)();
			expect([get(paneCollapsed), get(sidebarCollapsed)]).toEqual([false, false]);
		}
	});

	it("keeps the person's own toggle made while History is open", () => {
		setWindowWidth(1920);
		const release = widenForHistory(380, 40);
		sidebarCollapsed.set(false);
		sidebarCollapsed.set(true);
		release();
		expect(get(sidebarCollapsed)).toBe(true);
	});

	it('keeps 45 characters a line beside the list: 45 x 6.98 px of text plus the list column, the gap and the editor padding is 666 px', () => {
		expect(HISTORY_BESIDE_CANVAS).toBe(666);
		expect(canvasWithPanesHidden(1176, 40)).toBeLessThan(HISTORY_BESIDE_CANVAS);
		expect(canvasWithPanesHidden(1177, 40)).toBeGreaterThanOrEqual(HISTORY_BESIDE_CANVAS);
	});

	it('is the line DocViewer.css puts the list beside the text at', () => {
		const css = readFileSync(resolve(process.cwd(), 'src/lib/components/DocViewer.css'), 'utf8');
		expect(css).toMatch(new RegExp(`@container doc-canvas \\(min-width: ${HISTORY_BESIDE_CANVAS}px\\) \\{[^}]*\\}\\s*\\.doc:has\\(\\.canvas-history-drawer\\) \\{`));
		expect(css).toContain(`@container doc-canvas (width < ${HISTORY_BESIDE_CANVAS}px)`);
	});

	it('measures the content box, which is what the canvas container queries', () => {
		const element = document.createElement('div');
		Object.defineProperty(element, 'clientWidth', { value: 1152 });
		element.style.paddingLeft = '20px';
		element.style.paddingRight = '20px';
		document.body.append(element);
		expect(contentWidth(element)).toEqual({ width: 1112, inset: 40 });
		element.remove();
	});
});
