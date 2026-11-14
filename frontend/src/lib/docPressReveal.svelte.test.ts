import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import type { EditorView } from '@codemirror/view';

const { pressSpan, visibleRegion, releasePlace, capSheetAboveText, placeAboveSheet, giveBackRoom } = vi.hoisted(() => ({
	pressSpan: vi.fn(), visibleRegion: vi.fn(), releasePlace: vi.fn(), capSheetAboveText: vi.fn(), placeAboveSheet: vi.fn(), giveBackRoom: vi.fn(),
}));
vi.mock('$lib/cm/comment-marks', () => ({ pressSpan }));
vi.mock('$lib/cm/visible-region', () => ({ visibleRegion }));
vi.mock('$lib/documentReadingPlace', () => ({ releasePlace }));
vi.mock('$lib/sheetClearance', () => ({ capSheetAboveText, placeAboveSheet, giveBackRoom }));

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
