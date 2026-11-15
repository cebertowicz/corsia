import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import type { EditorView } from '@codemirror/view';

const { pressSpan, visibleRegion, releasePlace, capSheetAboveText, placeAboveSheet, giveBackRoom } = vi.hoisted(() => ({
	pressSpan: vi.fn(), visibleRegion: vi.fn(), releasePlace: vi.fn(), capSheetAboveText: vi.fn(), placeAboveSheet: vi.fn(), giveBackRoom: vi.fn(),
}));
vi.mock('$lib/cm/comment-marks', () => ({ pressSpan }));
vi.mock('$lib/cm/visible-region', () => ({ visibleRegion }));
vi.mock('$lib/documentReadingPlace', () => ({ releasePlace }));
// The real raised-sheet record, so the test pulls a sheet up as AdaptiveDrawer does; the cap and the placement are watched.
vi.mock('$lib/sheetClearance', async (actual) => ({ ...(await actual<typeof import('$lib/sheetClearance')>()), capSheetAboveText, placeAboveSheet, giveBackRoom }));

import { liftSheetCap, sheetCapped, sheetRaised } from '$lib/sheetClearance';
import { createSheetDrag } from '$lib/sheetDrag.svelte';

import { createPressReveal } from './docPressReveal.svelte';

let stop: (() => void) | null = null;
const view = { posAtCoords: () => 12 } as unknown as EditorView;
const scroller = document.createElement('div');
const span = { top: 300, bottom: 321, height: 21, regionTop: 100 };

function setup() {
	const state = $state({ sheetUp: true });
	let reveal!: ReturnType<typeof createPressReveal>;
	stop = $effect.root(() => {
		reveal = createPressReveal({
			highlights: () => [{ id: 'c1', from: 10, to: 30 }], view: () => view, draftCovered: () => true, scrollTo: vi.fn(), draft: () => null, sheetUp: () => state.sheetUp,
		});
	});
	flushSync();
	return { reveal, state };
}

afterEach(() => { stop?.(); stop = null; vi.clearAllMocks(); });

describe('createPressReveal', () => {
	it('caps the sheet above the pressed text and puts that text in the band above the sheet, from where the press landed', () => {
		pressSpan.mockReturnValue(span);
		visibleRegion.mockReturnValue({ scroller, band: { top: 100, bottom: 500 } });
		const panel = document.createElement('div');
		const { reveal } = setup();
		reveal.notePress(['c1'], new MouseEvent('click', { detail: 1 }));
		reveal.revealCard(true, panel);
		expect(pressSpan).toHaveBeenCalledWith(view, { from: 10, to: 30 }, 12);
		expect(capSheetAboveText).toHaveBeenCalledWith(panel, span);
		expect(placeAboveSheet).toHaveBeenCalledWith(scroller, span, { top: 100, sheetTop: 500 }, true);
		expect(releasePlace).toHaveBeenCalledWith(view);
	});

	it('moves nothing once the reader pulls the sheet up: not the growth itself, not the keyboard opening after it', () => {
		pressSpan.mockReturnValue(span);
		visibleRegion.mockReturnValue({ scroller, band: { top: 100, bottom: 500 } });
		const panel = document.createElement('div');
		const { reveal } = setup();
		reveal.notePress(['c1'], new MouseEvent('click', { detail: 1 }));
		reveal.revealCard(true, panel);
		expect(placeAboveSheet).toHaveBeenCalledTimes(1);
		liftSheetCap(panel);
		// The sheet's growth (its ResizeObserver tick), then the keyboard opening under Reply (a visual viewport resize): each re-runs the reveal.
		reveal.revealCard(false, panel);
		visibleRegion.mockReturnValue({ scroller, band: { top: 100, bottom: 300 } });
		reveal.revealCard(false, panel);
		expect(capSheetAboveText).toHaveBeenCalledTimes(1);
		expect(placeAboveSheet).toHaveBeenCalledTimes(1);
		expect(releasePlace).toHaveBeenCalledTimes(1);
	});

	it('a tap on the grabber of a short thread under a tall cap raises nothing, so the keyboard opening after it still lifts the text', () => {
		pressSpan.mockReturnValue(span);
		visibleRegion.mockReturnValue({ scroller, band: { top: 100, bottom: 500 } });
		// A short thread: 150 px of content under a 500 px cap, so the cap holds nothing back.
		const panel = document.createElement('div');
		panel.style.setProperty('--drawer-text-cap', '500px');
		panel.getBoundingClientRect = () => ({ top: 702, bottom: 852, left: 0, right: 393, width: 393, height: 150, x: 0, y: 702, toJSON: () => ({}) });
		const { reveal } = setup();
		reveal.notePress(['c1'], new MouseEvent('click', { detail: 1 }));
		reveal.revealCard(true, panel);
		// The grabber as AdaptiveDrawer wires it for a sheet with no half detent.
		const onExpand = vi.fn(() => liftSheetCap(panel));
		let drag!: ReturnType<typeof createSheetDrag>;
		const stopDrag = $effect.root(() => { drag = createSheetDrag({ expandable: () => sheetCapped(panel), height: () => 150, onExpand, onDismiss: vi.fn() }); });
		const handle = document.createElement('div');
		drag.start({ type: 'pointerdown', clientY: 760, pointerId: 3, button: 0, currentTarget: handle } as unknown as PointerEvent);
		drag.finish({ type: 'pointerup', clientY: 760, pointerId: 3, button: 0, currentTarget: handle } as unknown as PointerEvent);
		stopDrag();
		expect(onExpand).not.toHaveBeenCalled();
		expect(sheetRaised(panel)).toBe(false);
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('500px');
		// The keyboard opens under Reply: the band above the sheet shrinks and the reveal runs again.
		visibleRegion.mockReturnValue({ scroller, band: { top: 100, bottom: 300 } });
		reveal.revealCard(false, panel);
		expect(placeAboveSheet).toHaveBeenCalledTimes(2);
		expect(placeAboveSheet).toHaveBeenLastCalledWith(scroller, span, { top: 100, sheetTop: 300 }, false);
	});

	it('places nothing for a comment that was not opened by a press, and forgets a press once its sheet is gone, giving back the room it added', () => {
		pressSpan.mockReturnValue(span);
		visibleRegion.mockReturnValue({ scroller, band: { top: 100, bottom: 500 } });
		const { reveal, state } = setup();
		reveal.revealCard(true, null);
		expect(placeAboveSheet).not.toHaveBeenCalled();
		reveal.notePress(['c1']);
		reveal.revealCard(true, null);
		expect(placeAboveSheet).toHaveBeenCalledTimes(1);
		state.sheetUp = false; flushSync();
		expect(giveBackRoom).toHaveBeenCalledWith(scroller);
		state.sheetUp = true; flushSync();
		reveal.revealCard(true, null);
		expect(placeAboveSheet).toHaveBeenCalledTimes(1);
	});
});
