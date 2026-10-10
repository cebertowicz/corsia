import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { A, B, append, hiddenOf, lane, trackRigs, type Rig } from './typed-reveal-rig';
import { setPresenceRanges } from './presence-marks';
import { settleTypedReveals } from './typed-reveal';
import { startArrivalSelect, type ArrivalSelect, type SelectOutcome } from './typed-reveal-arrival';

const { make, cleanup } = trackRigs();
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); document.body.innerHTML = ''; });

const TEXT = 'Say hello brave world.';
const SWEEP = 200 + 6 * 11;
const request = (extras: Partial<ArrivalSelect> = {}): ArrivalSelect => ({
	key: 'comment-1', threadId: A.threadId, name: A.name, color: '#8b5cf6', from: 4, to: 15, sweepMs: SWEEP, holdMs: 1_000, ...extras,
});
const selectedRange = (r: Rig) => [...r.view.dom.querySelectorAll('.cm-presence-select')].map((el) => el.textContent).join('');

/** Start a select and record how its promise settles, and how many times. */
function run(r: Rig, extras: Partial<ArrivalSelect> = {}) {
	const outcomes: SelectOutcome[] = [];
	const promise = startArrivalSelect(r.view, request(extras));
	promise?.then((outcome) => outcomes.push(outcome));
	return { promise, outcomes };
}
const settle = () => vi.advanceTimersByTimeAsync(0);
const presenceOf = (...ids: string[]) => setPresenceRanges.of(ids.map((threadId) => ({
	threadId, name: threadId, color: '#000', span: { from: 4, to: 4 }, highlightSpan: { from: 4, to: 15 },
})));

describe('comment select: one owner, one promise per arrival', () => {
	it('sweeps the anchor over the sweep time, holds, then settles ended and clears the highlight', async () => {
		const r = make(TEXT);
		const { promise, outcomes } = run(r);
		expect(promise).not.toBeNull();
		expect(lane(r, A.threadId)!.select).toMatchObject({ key: 'comment-1', from: 4, to: 15 });
		const widths: number[] = [];
		for (let at = 0; at < SWEEP + 300; at += 16) { widths.push(selectedRange(r).length); vi.advanceTimersByTime(16); }
		expect(widths[0]).toBeLessThan(11);
		expect(widths).toEqual([...widths].sort((a, b) => a - b));
		expect(widths[widths.length - 1]).toBe(11);
		// The highlight names its agent, so a page can find that agent's pending anchor.
		expect([...r.view.dom.querySelectorAll('.cm-presence-select')].map((el) => el.getAttribute('data-thread-id'))).toContain(A.threadId);
		await settle();
		expect(outcomes).toEqual([]);
		vi.advanceTimersByTime(SWEEP + 1_000);
		await settle();
		expect(outcomes).toEqual(['ended']);
		expect(selectedRange(r)).toBe('');
		expect(lane(r, A.threadId)).toBeUndefined();
	});

	it('cannot run when typing is not allowed, the span is empty, or the doc has no such span', () => {
		const r = make(TEXT);
		r.options.canType = false;
		expect(startArrivalSelect(r.view, request())).toBeNull();
		expect(lane(r, A.threadId)).toBeUndefined();
		r.options.canType = true;
		expect(startArrivalSelect(r.view, request({ from: 6, to: 6 }))).toBeNull();
		expect(startArrivalSelect(r.view, request({ from: 400, to: 410 }))).toBeNull();
		expect(lane(r, A.threadId)).toBeUndefined();
	});

	it("another actor's edit before the anchor shifts the span and keeps the same select, still paused", async () => {
		const r = make(TEXT);
		const { outcomes } = run(r);
		vi.advanceTimersByTime(100);
		const before = lane(r, A.threadId)!.select!;
		r.edit(B, (t) => t.insert(0, 'XX '));
		const after = lane(r, A.threadId)!.select!;
		expect(after.key).toBe(before.key);
		expect([after.from, after.to]).toEqual([before.from + 3, before.to + 3]);
		expect(after.shown).toBeGreaterThanOrEqual(before.shown);
		vi.advanceTimersByTime(SWEEP + 1_100);
		await settle();
		expect(outcomes).toEqual(['ended']);
	});

	it('a presence re-dispatch for any agent neither restarts nor re-arms the select', async () => {
		const r = make(TEXT);
		const { outcomes } = run(r);
		vi.advanceTimersByTime(400);
		const before = lane(r, A.threadId)!.select!;
		r.view.dispatch({ effects: presenceOf(A.threadId, B.threadId) });
		expect(lane(r, A.threadId)!.select).toEqual(before);
		expect(lane(r, B.threadId)).toBeUndefined();
		r.view.dispatch({ effects: setPresenceRanges.of([]) });
		expect(lane(r, A.threadId)!.select).toEqual(before);
		vi.advanceTimersByTime(SWEEP + 1_100);
		await settle();
		expect(outcomes).toEqual(['ended']);
	});

	it('a user click inside the span settles interrupted and ends it at once; a click elsewhere does not', async () => {
		const r = make(TEXT);
		const { outcomes } = run(r);
		vi.advanceTimersByTime(100);
		r.view.dispatch({ selection: { anchor: 20 }, userEvent: 'select.pointer' });
		await settle();
		expect(outcomes).toEqual([]);
		r.view.dispatch({ selection: { anchor: 8 }, userEvent: 'select.pointer' });
		await settle();
		expect(outcomes).toEqual(['interrupted']);
		expect(selectedRange(r)).toBe('');
	});

	it("a user edit touching the span, or the same agent's pending typed text, settles interrupted", async () => {
		const first = make(TEXT);
		const one = run(first);
		first.view.dispatch({ changes: { from: 6, to: 7, insert: 'E' }, userEvent: 'input.type' });
		await settle();
		expect(one.outcomes).toEqual(['interrupted']);

		const second = make(TEXT);
		second.edit(A, append(' Typed later by the agent, slowly and at length.'));
		const two = run(second);
		const pending = lane(second, A.threadId)!.segments[0];
		second.view.dispatch({ selection: { anchor: pending.from + 2 }, userEvent: 'select.pointer' });
		await settle();
		expect(two.outcomes).toEqual(['interrupted']);
	});

	it('settles unavailable when another actor deletes the whole anchor', async () => {
		const r = make(TEXT);
		const { outcomes } = run(r);
		r.edit(B, (t) => t.delete(3, 13));
		await settle();
		expect(outcomes).toEqual(['unavailable']);
		expect(lane(r, A.threadId)).toBeUndefined();
	});

	it('settles unavailable when the lane is settled or the editor is destroyed', async () => {
		const settled = make(TEXT);
		const a = run(settled);
		settleTypedReveals(settled.view);
		await settle();
		expect(a.outcomes).toEqual(['unavailable']);

		const destroyed = make(TEXT);
		const b = run(destroyed);
		destroyed.view.destroy();
		await settle();
		expect(b.outcomes).toEqual(['unavailable']);
	});

	it('settles exactly once however the select ends', async () => {
		const r = make(TEXT);
		const { outcomes } = run(r);
		r.view.dispatch({ selection: { anchor: 8 }, userEvent: 'select.pointer' });
		settleTypedReveals(r.view);
		vi.advanceTimersByTime(5_000);
		r.view.destroy();
		await settle();
		expect(outcomes).toEqual(['interrupted']);
	});

	it('typing paused by the select resumes where it was after the select ends', async () => {
		const r = make(TEXT);
		r.edit(A, append(' More words typed out slowly.'.repeat(6)));
		vi.advanceTimersByTime(1_200);
		const before = hiddenOf(r, A.threadId);
		expect(before).toBeGreaterThan(0);
		const { outcomes } = run(r, { holdMs: 600 });
		vi.advanceTimersByTime(500);
		expect(hiddenOf(r, A.threadId)).toBe(before);
		vi.advanceTimersByTime(SWEEP + 600);
		await settle();
		expect(outcomes).toEqual(['ended']);
		vi.advanceTimersByTime(300);
		expect(hiddenOf(r, A.threadId)).toBeLessThan(before);
	});
});

describe('one select slot per lane', () => {
	it('a same-agent replacement during a comment select skips its ghost and types after the select', async () => {
		const r = make(TEXT);
		const { outcomes } = run(r);
		vi.advanceTimersByTime(300);
		const before = lane(r, A.threadId)!.select!;
		r.edit(A, (t) => { t.delete(16, 5); t.insert(16, 'planet'); });
		const after = lane(r, A.threadId)!.select!;
		expect(after.key).toBe(before.key);
		expect(r.view.dom.querySelector('.cm-presence-ghost')).toBeNull();
		expect(hiddenOf(r, A.threadId)).toBe('planet'.length);
		vi.advanceTimersByTime(SWEEP + 1_100);
		await settle();
		expect(outcomes).toEqual(['ended']);
		vi.advanceTimersByTime(3_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});

	it("cannot start a comment select while the lane's ghost is running", () => {
		const r = make(TEXT);
		r.edit(A, (t) => { t.delete(4, 11); t.insert(4, 'goodbye'); });
		expect(lane(r, A.threadId)!.select?.ghost).toBe('hello brave');
		expect(startArrivalSelect(r.view, request())).toBeNull();
		expect(lane(r, A.threadId)!.select?.ghost).toBe('hello brave');
	});

	it("touching a ghost ends only the ghost: another agent's comment select keeps running", async () => {
		const r = make(TEXT);
		const { outcomes } = run(r, { key: 'comment-b', threadId: B.threadId, name: B.name, from: 0, to: 3 });
		r.edit(A, (t) => { t.delete(8, 5); t.insert(8, 'goodbye'); });
		expect(lane(r, A.threadId)!.select?.ghost).toBeTruthy();
		r.view.dispatch({ selection: { anchor: 8 }, userEvent: 'select.pointer' });
		await settle();
		expect(lane(r, A.threadId)?.select ?? null).toBeNull();
		expect(lane(r, B.threadId)!.select?.key).toBe('comment-b');
		expect(outcomes).toEqual([]);
	});

	it("two agents' comment arrivals in one document run in their own lanes", async () => {
		const r = make(TEXT);
		const a = run(r, { key: 'comment-a', holdMs: 400 });
		const b = run(r, { key: 'comment-b', threadId: B.threadId, name: B.name, from: 16, to: 21, sweepMs: 230, holdMs: 2_000 });
		expect(lane(r, A.threadId)!.select!.key).toBe('comment-a');
		expect(lane(r, B.threadId)!.select!.key).toBe('comment-b');
		r.view.dispatch({ selection: { anchor: 6 }, userEvent: 'select.pointer' });
		await settle();
		expect(a.outcomes).toEqual(['interrupted']);
		expect(b.outcomes).toEqual([]);
		vi.advanceTimersByTime(230 + 2_000 + 100);
		await settle();
		expect(b.outcomes).toEqual(['ended']);
	});
});
