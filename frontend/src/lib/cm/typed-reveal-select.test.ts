import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { A, hiddenOf, lane, trackRigs, type Rig } from './typed-reveal-rig';

const { make, cleanup } = trackRigs();
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); document.body.innerHTML = ''; });

const ghostText = (r: Rig) => r.view.dom.querySelector('.cm-presence-ghost')?.textContent ?? null;
const selectedGhost = (r: Rig) => r.view.dom.querySelector('.cm-presence-ghost-selected')?.textContent ?? '';

/** Step 16 ms frames, recording what is on screen at each one. */
function frames(r: Rig, ms: number, id = A.threadId) {
	const seen: Array<{ at: number; ghost: string | null; selected: string; hidden: number }> = [];
	for (let at = 0; at <= ms; at += 16) {
		seen.push({ at, ghost: ghostText(r), selected: selectedGhost(r), hidden: hiddenOf(r, id) });
		vi.advanceTimersByTime(16);
	}
	return seen;
}

describe('agent replacement: select the old text, hold, remove, then type', () => {
	it('shows the old text selected first, holds 400-600 ms, removes it at once, then types the new text', () => {
		const r = make('Say hello brave world.');
		r.edit(A, (t) => { t.delete(4, 11); t.insert(4, 'goodbye'); });
		expect(r.view.state.doc.toString()).toBe('Say goodbye world.');
		const seen = frames(r, 2_500);
		const first = seen[0];
		expect(first.ghost).toBe('hello brave');
		expect(first.hidden).toBe('goodbye'.length);
		const removedAt = seen.find((frame) => frame.ghost === null)!;
		expect(removedAt).toBeTruthy();
		// Sweep: the selected part grows frame by frame and reaches the whole old text.
		const widths = seen.filter((f) => f.ghost !== null).map((f) => f.selected.length);
		expect(widths).toEqual([...widths].sort((a, b) => a - b));
		expect(Math.max(...widths)).toBe('hello brave'.length);
		const sweepEnd = seen.find((f) => f.selected.length === 'hello brave'.length)!.at;
		const hold = removedAt.at - sweepEnd;
		expect(hold).toBeGreaterThanOrEqual(400 - 16);
		expect(hold).toBeLessThanOrEqual(600 + 32);
		// Nothing of the new text shows while the old text is selected.
		expect(seen.filter((f) => f.ghost !== null).every((f) => f.hidden === 'goodbye'.length)).toBe(true);
		// Then the new text types in, finishing with nothing hidden.
		expect(seen.filter((f) => f.at > removedAt.at).some((f) => f.hidden > 0 && f.hidden < 'goodbye'.length)).toBe(true);
		expect(seen[seen.length - 1].hidden).toBe(0);
		expect(r.view.state.doc.toString()).toBe('Say goodbye world.');
	});

	it('a pure deletion selects, holds, then removes, with nothing typed', () => {
		const r = make('Keep this and drop that part.');
		r.edit(A, (t) => t.delete(9, 9));
		const seen = frames(r, 1_500);
		expect(seen[0].ghost).toBe(' and drop');
		expect(seen.some((f) => f.ghost === null)).toBe(true);
		const removedAt = seen.find((f) => f.ghost === null)!.at;
		expect(removedAt).toBeGreaterThanOrEqual(9 * 6 + 200 + 400 - 32);
		expect(r.view.state.doc.toString()).toBe('Keep this that part.');
	});

	it('a pure insertion has no selection step', () => {
		const r = make('Start. ');
		r.edit(A, (t) => t.insert(t.length, 'More words here.'));
		expect(ghostText(r)).toBeNull();
		expect(lane(r, A.threadId)!.select).toBeNull();
	});

	it('the ghost is display-only: not document text and not selectable', () => {
		const r = make('Say hello brave world.');
		r.edit(A, (t) => { t.delete(4, 11); t.insert(4, 'goodbye'); });
		expect(r.view.state.doc.toString()).not.toContain('brave');
		expect(r.view.state.doc.toString()).toBe(r.text.toString());
		const ghost = r.view.dom.querySelector('.cm-presence-ghost')!;
		expect(ghost.getAttribute('aria-hidden')).toBe('true');
	});

	it('a user click at the replaced spot ends the sequence at once with the final state', () => {
		const r = make('Say hello brave world.');
		r.edit(A, (t) => { t.delete(4, 11); t.insert(4, 'goodbye'); });
		vi.advanceTimersByTime(100);
		expect(ghostText(r)).not.toBeNull();
		r.view.dispatch({ selection: { anchor: 6 }, userEvent: 'select.pointer' });
		expect(ghostText(r)).toBeNull();
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});

	it('shows the final state at once when typing is not allowed (reduced motion)', () => {
		const r = make('Say hello brave world.');
		r.options.canType = false;
		r.edit(A, (t) => { t.delete(4, 11); t.insert(4, 'goodbye'); });
		expect(ghostText(r)).toBeNull();
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});
});
