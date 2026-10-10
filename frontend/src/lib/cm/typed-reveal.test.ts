import { EditorState } from '@codemirror/state';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { A, B, TEXT, append, hiddenOf, lane, trackRigs } from './typed-reveal-rig';
import { createTypedReveal, revealTypedThrough, revealTypedUnder, settleTypedReveals, typedReveals } from './typed-reveal';
import { hiddenCount } from './typed-reveal-segments';

const { make, cleanup } = trackRigs();
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); document.body.innerHTML = ''; });

describe('yCollab applies a remote update synchronously with the author', () => {
	it('row 1: the editor transaction exists before applyUpdate returns and hides the insert', () => {
		const r = make();
		r.edit(A, append('Hello from Alpha.'));
		expect(r.view.state.doc.toString()).toBe(r.text.toString());
		expect(hiddenOf(r, A.threadId)).toBe('Hello from Alpha.'.length);
	});

	it('row 1: an update with no author never hides anything', () => {
		const r = make();
		r.edit(null, append('missed while away'));
		expect(r.lanes()).toHaveLength(0);
	});
});

describe('typing lanes', () => {
	it('row 1: the caret types the text out and then idles', () => {
		const r = make();
		r.edit(A, append(TEXT));
		expect(hiddenOf(r, A.threadId)).toBe(TEXT.length);
		vi.advanceTimersByTime(13_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
		expect(lane(r, A.threadId)?.caret).not.toBeNull();
	});

	it('row 2: a second edit joins the backlog and the already shown text does not jump back', () => {
		const r = make();
		r.edit(A, append(TEXT));
		vi.advanceTimersByTime(3_000);
		const mid = hiddenOf(r, A.threadId);
		expect(mid).toBeGreaterThan(0);
		expect(mid).toBeLessThan(TEXT.length);
		r.edit(A, append('more'));
		expect(hiddenOf(r, A.threadId)).toBe(mid + 4);
		vi.advanceTimersByTime(13_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});

	it('row 3: two ranges in one update type in document order', () => {
		const r = make('aaa bbb');
		r.edit(A, (t) => { t.insert(3, ' ONE'); t.insert(t.length, ' TWO'); });
		const segments = lane(r, A.threadId)!.segments;
		expect(segments).toHaveLength(2);
		expect(segments[0].from).toBeLessThan(segments[1].from);
		vi.advanceTimersByTime(13_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});

	it('row 4: two agents type concurrently', () => {
		const r = make();
		r.edit(A, append(' alpha alpha alpha'));
		r.edit(B, (t) => t.insert(0, 'beta beta beta '));
		expect(r.lanes()).toHaveLength(2);
		vi.advanceTimersByTime(1_000);
		const a = hiddenOf(r, A.threadId);
		const b = hiddenOf(r, B.threadId);
		expect(a).toBeLessThan(' alpha alpha alpha'.length);
		expect(b).toBeLessThan('beta beta beta '.length);
	});

	it('row 5: an insert inside another agent hidden span shows at once and splits it', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		r.edit(B, (t) => t.insert(5, 'BBB'));
		expect(hiddenOf(r, B.threadId)).toBe(0);
		expect(hiddenOf(r, A.threadId)).toBe(10);
		expect(lane(r, A.threadId)!.segments).toHaveLength(2);
		const visible = r.view.state.doc.toString();
		expect(visible).toContain('BBB');
	});

	it('row 6: a deletion inside the span shows and the rest keeps typing', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		r.edit(B, (t) => t.delete(3, 4));
		expect(hiddenOf(r, A.threadId)).toBe(6);
		vi.advanceTimersByTime(13_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});

	it('row 7: the agent deleting its own unrevealed text shrinks the backlog', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		r.edit(A, (t) => t.delete(2, 5));
		expect(hiddenOf(r, A.threadId)).toBe(5);
	});

	it('row 29: a lane covering its own anchor can be revealed through the anchor', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		revealTypedThrough(r.view, 6);
		expect(hiddenOf(r, A.threadId)).toBe(4);
		expect(lane(r, A.threadId)!.segments[0].from).toBe(6);
	});

	it('row 8: revealing through an anchor leaves other lanes alone past that point', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		r.edit(B, (t) => t.insert(0, 'xyz'));
		revealTypedThrough(r.view, 2);
		expect(lane(r, A.threadId)).toBeDefined();
		expect(hiddenOf(r, A.threadId)).toBe(10);
	});

	it('a highlight or scroll target over hidden text shows that text at once', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		revealTypedUnder(r.view, [{ from: 3, to: 5 }]);
		expect(hiddenOf(r, A.threadId)).toBe(5);
		expect(lane(r, A.threadId)!.segments[0].from).toBe(5);
	});

	it('a highlight away from hidden text leaves typing alone', () => {
		const r = make('before ');
		r.edit(A, append('0123456789'));
		revealTypedUnder(r.view, [{ from: 0, to: 6 }, { from: 2, to: 2 }]);
		expect(hiddenOf(r, A.threadId)).toBe(10);
	});

	it('row 27: typing finishes on its curve with no further input, then the idle caret expires', () => {
		const r = make();
		r.edit(A, append(TEXT));
		vi.advanceTimersByTime(13_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
		expect(lane(r, A.threadId)).toBeDefined();
		vi.advanceTimersByTime(6 * 60_000);
		expect(lane(r, A.threadId)).toBeUndefined();
	});

	it('row 27: a long backlog finishes inside the 12 s cap', () => {
		const r = make();
		r.edit(A, append('x'.repeat(3_000)));
		vi.advanceTimersByTime(12_500);
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});
});

describe('caret presentation', () => {
	it('row 1: the idle caret blinks and the typing caret holds steady (CSS)', () => {
		const r = make();
		const css = Array.from(document.head.querySelectorAll('style')).map((s) => s.textContent).join('');
		expect(css).toContain('cm-presence-blink');
		expect(css).toContain('cm-presence-cursor-typing');
		expect(css).toMatch(/prefers-reduced-motion/);
		void r;
	});

	it('row 26: the name label is clamped inside the editor edge', async () => {
		const r = make();
		r.edit(A, append('hello'));
		vi.advanceTimersByTime(13_000);
		const label = r.view.dom.querySelector<HTMLElement>('.cm-presence-cursor-label');
		expect(label).not.toBeNull();
		vi.spyOn(r.view.scrollDOM, 'getBoundingClientRect').mockReturnValue({ left: 0, right: 315 } as DOMRect);
		Object.defineProperty(r.view.scrollDOM, 'clientWidth', { configurable: true, value: 300 });
		vi.spyOn(label!, 'getBoundingClientRect').mockReturnValue({ right: 340 } as DOMRect);
		r.view.requestMeasure();
		await vi.advanceTimersByTimeAsync(50);
		r.view.dispatch({ changes: { from: 0, insert: ' ' } });
		await vi.advanceTimersByTimeAsync(50);
		const moved = r.view.dom.querySelector<HTMLElement>('.cm-presence-cursor-label')!;
		expect(moved.style.transform).toBe('translateX(-40px)');
	});
});
