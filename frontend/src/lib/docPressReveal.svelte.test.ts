import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import type { EditorView } from '@codemirror/view';

const { pressSpan, clearRevealRoom } = vi.hoisted(() => ({ pressSpan: vi.fn(), clearRevealRoom: vi.fn() }));
vi.mock('$lib/cm/comment-marks', () => ({ pressSpan, clearRevealRoom }));
vi.mock('$lib/sheetClearance', () => ({ capSheetAboveText: vi.fn() }));

import { createPressReveal } from './docPressReveal.svelte';

let stop: (() => void) | null = null;
const view = { posAtCoords: () => 12 } as unknown as EditorView;

function setup() {
	const state = $state({ sheetUp: true });
	const scrollTo = vi.fn();
	const draftCovered = vi.fn(() => true);
	let reveal!: ReturnType<typeof createPressReveal>;
	stop = $effect.root(() => {
		reveal = createPressReveal({
			highlights: () => [{ id: 'c1', from: 10, to: 30 }], view: () => view, draftCovered, scrollTo, draft: () => null, sheetUp: () => state.sheetUp,
		});
	});
	flushSync();
	return { reveal, state, scrollTo, draftCovered };
}

afterEach(() => { stop?.(); stop = null; vi.clearAllMocks(); });

describe('createPressReveal', () => {
	it('lifts the pressed highlight above the sheet from where the press landed', () => {
		pressSpan.mockReturnValue({ from: 10, to: 30 });
		const { reveal, scrollTo } = setup();
		reveal.notePress(['c1'], new MouseEvent('click', { detail: 1 }));
		reveal.revealCard(true, null);
		expect(pressSpan).toHaveBeenCalledWith(view, { from: 10, to: 30 }, 12);
		expect(scrollTo).toHaveBeenCalledWith({ from: 10, to: 30, origin: 'press-reveal' });
	});

	it('places nothing for a comment that was not opened by a press, and forgets a press once its sheet is gone', () => {
		pressSpan.mockReturnValue({ from: 10, to: 30 });
		const { reveal, state, scrollTo } = setup();
		reveal.revealCard(true, null);
		expect(scrollTo).not.toHaveBeenCalled();
		reveal.notePress(['c1']);
		state.sheetUp = false; flushSync();
		state.sheetUp = true; flushSync();
		reveal.revealCard(true, null);
		expect(scrollTo).not.toHaveBeenCalled();
		expect(clearRevealRoom).toHaveBeenCalledWith(view);
	});
});
