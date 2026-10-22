import { EditorSelection } from '@codemirror/state';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { selectSweepMs } from '$lib/typingPace';
import { A, lane, trackRigs, type Rig } from './typed-reveal-rig';
import { endPreview, previewSlot, type PreviewMode } from './typed-reveal-preview';
import { startArrivalSelect, type ArrivalSelect, type SelectOutcome } from './typed-reveal-arrival';
import { commentMarks, setCommentRanges } from './comment-marks';
import { suggestionMarks } from './suggestion-marks';
import { CLAIM_GRACE_MS } from '$lib/stores/livePreviewFeed';

const { make, cleanup } = trackRigs();
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); document.body.innerHTML = ''; });

const DOC = 'Hello world. The rest stays.';
const WORLD = { from: 6, to: 11 };
const SWEEP = selectSweepMs(WORLD.to - WORLD.from);
const HOLD = 1_000;

function stream(r: Rig, mode: PreviewMode, text = '', callId = 'call-1') {
	r.view.dispatch({ effects: previewSlot.of({
		callId, threadId: A.threadId, name: A.name, slot: 0, from: WORLD.from, to: WORLD.to, text, mode,
	}) });
}

/** The comment's arrival, naming the call that posted it, as the page starts it once the comment is loaded. */
function arrive(r: Rig, extras: Partial<ArrivalSelect> = {}) {
	const outcomes: SelectOutcome[] = [];
	const promise = startArrivalSelect(r.view, {
		key: 'comment-1', threadId: A.threadId, name: A.name, color: '#8b5cf6', ...WORLD,
		sweepMs: SWEEP, holdMs: HOLD, callId: 'call-1', ...extras,
	});
	promise?.then((outcome) => outcomes.push(outcome));
	return { promise, outcomes };
}

const settle = () => vi.advanceTimersByTimeAsync(0);
const selected = (r: Rig) => [...r.view.dom.querySelectorAll('.cm-presence-select')].map((el) => el.textContent).join('');
const inserts = (r: Rig) => [...r.view.dom.querySelectorAll<HTMLElement>('.cm-suggestion-insert')].map((el) => el.getAttribute('data-insert'));
const strikes = (r: Rig) => [...r.view.dom.querySelectorAll('.cm-suggestion-del')].map((el) => el.textContent);
const pointer = () => ({ focus: () => {}, hover: () => {} });
const committed = (r: Rig, insert: string) => r.view.dispatch({ effects: setCommentRanges.of([{
	...WORLD, id: 'comment-1', suggestion: { quote: 'world', insert, color: '#8b5cf6', inline: true },
}]) });

describe('a streamed comment is posted, never typed', () => {
	it('highlights its anchor in the agent colour with the caret there, and holds while the body is written', () => {
		const r = make(DOC);
		stream(r, 'comment', 'never drawn');
		expect(lane(r, A.threadId)?.select).toMatchObject({ from: WORLD.from, to: WORLD.to });
		vi.advanceTimersByTime(60_000);
		expect(selected(r)).toBe('world');
		expect(r.view.dom.querySelector('.cm-presence-preview')).toBeNull();
		expect(r.view.dom.textContent).not.toContain('never drawn');
		expect(r.view.state.doc.toString()).toBe(DOC);
	});

	it('on commit the card waits the body hold counted from the highlight, not from the commit', async () => {
		const r = make(DOC);
		stream(r, 'comment');
		vi.advanceTimersByTime(300);
		const { promise, outcomes } = arrive(r);
		expect(promise).not.toBeNull();
		vi.advanceTimersByTime(SWEEP + HOLD - 300 - 50);
		await settle();
		expect(outcomes).toEqual([]);
		vi.advanceTimersByTime(100);
		await settle();
		expect(outcomes).toEqual(['ended']);
		expect(selected(r)).toBe('');
	});

	it('a slow model adds no second wait: a commit after the hold has passed shows the card at once', async () => {
		const r = make(DOC);
		stream(r, 'comment');
		vi.advanceTimersByTime(10_000);
		const { outcomes } = arrive(r);
		vi.advanceTimersByTime(50);
		await settle();
		expect(outcomes).toEqual(['ended']);
	});

	it('clicking the pending anchor ends the highlight, and the committed card then shows at once', async () => {
		const r = make(DOC);
		stream(r, 'comment');
		vi.advanceTimersByTime(500);
		r.view.dispatch({ selection: EditorSelection.cursor(8), userEvent: 'select' });
		expect(selected(r)).toBe('');
		const { outcomes } = arrive(r);
		await settle();
		expect(outcomes).toEqual(['interrupted']);
		expect(lane(r, A.threadId)?.previews ?? []).toHaveLength(0);
	});

	it('a failed or uncommitted call removes the highlight and posts nothing', () => {
		const r = make(DOC);
		stream(r, 'comment');
		vi.advanceTimersByTime(500);
		r.view.dispatch({ effects: endPreview.of({ callId: 'call-1' }) });
		expect(selected(r)).toBe('');
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
		expect(lane(r, A.threadId)?.select ?? null).toBeNull();
	});
});

describe('a streamed suggestion types its insert inline', () => {
	it('selects the quote, then types the insert after it in suggestion marks, leaving the document alone', () => {
		const r = make(DOC);
		stream(r, 'suggest', 'there');
		vi.advanceTimersByTime(8_000);
		expect(strikes(r)).toEqual(['world']);
		expect(inserts(r)).toEqual(['there']);
		expect(r.view.dom.querySelector('.cm-presence-preview')).toBeNull();
		expect(r.view.state.doc.toString()).toBe(DOC);
	});

	it('hands over on commit: the committed insert shows what was typed, and the lane types the rest', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		stream(r, 'suggest', 'there');
		vi.advanceTimersByTime(8_000);
		const { outcomes } = arrive(r, { insert: 'there, friend' });
		await settle();
		expect(outcomes).toEqual(['ended']);
		committed(r, 'there, friend');
		// One insert: the committed suggestion's, showing only what had typed. Nothing types twice.
		expect(inserts(r)).toEqual(['there']);
		expect(strikes(r)).toEqual(['world']);
		vi.advanceTimersByTime(8_000);
		expect(inserts(r)).toEqual(['there, friend']);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
	});

	it('the preview keeps drawing until the committed suggestion is shown, so nothing blinks', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		stream(r, 'suggest', 'there');
		vi.advanceTimersByTime(8_000);
		arrive(r, { insert: 'there' });
		await settle();
		vi.advanceTimersByTime(1_000);
		expect(inserts(r)).toEqual(['there']);
		committed(r, 'there');
		expect(inserts(r)).toEqual(['there']);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
	});

	it('a suggestion that loads before its arrival frame is taken over at once, never drawn twice', () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		stream(r, 'suggest', 'there');
		vi.advanceTimersByTime(8_000);
		committed(r, 'there, friend');
		expect(inserts(r)).toEqual(['there']);
		vi.advanceTimersByTime(8_000);
		expect(inserts(r)).toEqual(['there, friend']);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
	});

	it('the arrival of a streamed suggestion runs no second select over the typing insert', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		stream(r, 'suggest', 'there');
		vi.advanceTimersByTime(8_000);
		committed(r, 'there, friend');
		const { outcomes } = arrive(r, { insert: 'there, friend' });
		await settle();
		expect(outcomes).toEqual(['ended']);
		expect(lane(r, A.threadId)?.select ?? null).toBeNull();
	});

	it('a committed suggestion that differs drops the stale preview and types the committed insert instead', () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		stream(r, 'suggest', 'there');
		vi.advanceTimersByTime(8_000);
		const { promise } = arrive(r, { insert: 'goodbye' });
		expect(promise).not.toBeNull();
		expect(r.lanes().flatMap((l) => l.previews)).toMatchObject([{ mode: 'suggest', text: 'goodbye', typed: 0 }]);
		committed(r, 'goodbye');
		expect(inserts(r)).not.toContain('goodbye');
		vi.advanceTimersByTime(8_000);
		expect(inserts(r)).toEqual(['goodbye']);
	});
});

describe('a suggestion that streamed nothing still types its insert', () => {
	const shownLengths = (r: Rig, until: number) => {
		const lengths = new Set<number>();
		for (let t = 0; t < until; t += 50) {
			vi.advanceTimersByTime(50);
			lengths.add((inserts(r)[0] ?? '').length);
		}
		return lengths;
	};

	it('types a committed suggestion that arrives with no preview, and the call id is not needed', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		const { outcomes } = arrive(r, { insert: 'there, friend', callId: null });
		await settle();
		committed(r, 'there, friend');
		expect(outcomes).toEqual(['ended']);
		expect(inserts(r)).not.toContain('there, friend');
		const lengths = shownLengths(r, 12_000);
		expect([...lengths].some((n) => n > 0 && n < 'there, friend'.length)).toBe(true);
		expect(inserts(r)).toEqual(['there, friend']);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
	});

	it('types a suggestion the editor placed away from the server offsets, instead of drawing it whole', async () => {
		// A busy document: the committed suggestion sits at the editor's own place, three characters from the arrival's.
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		const insert = 'there, friend';
		const { outcomes } = arrive(r, { insert, from: 6, to: 11 });
		await settle();
		r.view.dispatch({ effects: setCommentRanges.of([{
			from: 9, to: 14, id: 'comment-1', suggestion: { quote: 'world', insert, color: '#8b5cf6', inline: true },
		}]) });
		expect(outcomes).toEqual(['ended']);
		expect(inserts(r)).not.toContain(insert);
		const lengths = shownLengths(r, 12_000);
		expect([...lengths].some((n) => n > 0 && n < insert.length)).toBe(true);
		expect(inserts(r)).toEqual([insert]);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
	});

	it('drops the typing when no suggestion ever commits, so nothing is left half drawn', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		arrive(r, { insert: 'there, friend' });
		await settle();
		vi.advanceTimersByTime(CLAIM_GRACE_MS);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
		expect(inserts(r)).toEqual([]);
	});

	it('shows the whole insert at once when typing is not allowed', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		r.options.canType = false;
		const { promise } = arrive(r, { insert: 'there, friend' });
		expect(promise).toBeNull();
		committed(r, 'there, friend');
		expect(inserts(r)).toEqual(['there, friend']);
	});

	it('a user click on the struck quote ends the typing and shows the whole insert', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(pointer)]);
		arrive(r, { insert: 'there, friend' });
		await settle();
		committed(r, 'there, friend');
		vi.advanceTimersByTime(2_000);
		r.view.dispatch({ selection: EditorSelection.cursor(8), userEvent: 'select' });
		expect(inserts(r)).toEqual(['there, friend']);
	});
});

describe('a call that turns from a comment into a suggestion', () => {
	it('starts over as a suggestion once the replacement begins', () => {
		const r = make(DOC);
		stream(r, 'comment');
		stream(r, 'suggest', 'th');
		const previews = r.lanes().flatMap((l) => l.previews);
		expect(previews).toHaveLength(1);
		expect(previews[0]).toMatchObject({ mode: 'suggest', text: 'th' });
		vi.advanceTimersByTime(8_000);
		expect(inserts(r)).toEqual(['th']);
	});
});
