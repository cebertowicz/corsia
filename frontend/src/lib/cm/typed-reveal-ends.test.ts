import { EditorState } from '@codemirror/state';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { A, B, TEXT, append, hiddenOf, lane, trackRigs } from './typed-reveal-rig';
import { createTypedReveal, revealTypedThrough, settleTypedReveals, typedReveals } from './typed-reveal';
import { hiddenCount } from './typed-reveal-segments';

const { make, cleanup } = trackRigs();
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); document.body.innerHTML = ''; });

describe('user actions and ends', () => {
	it('row 17: a user keystroke inside the span shows it at once', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		r.view.dispatch({ changes: { from: 4, insert: 'u' }, userEvent: 'input.type' });
		expect(lane(r, A.threadId)?.segments ?? []).toHaveLength(0);
	});

	it('row 17: a click (selection) inside the span shows it', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		r.view.dispatch({ selection: { anchor: 4 }, userEvent: 'select.pointer' });
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});

	it('row 18: typing elsewhere does not end typing and the user text is never hidden', () => {
		const r = make('Start. ');
		r.edit(A, append('0123456789'));
		r.view.dispatch({ changes: { from: 0, insert: 'u' }, userEvent: 'input.type' });
		expect(hiddenOf(r, A.threadId)).toBe(10);
		expect(r.view.state.doc.toString().startsWith('uStart')).toBe(true);
	});

	it('row 19: select all shows every touched span', () => {
		const r = make('');
		r.edit(A, append('0123456789'));
		r.edit(B, (t) => t.insert(0, 'beta'));
		r.view.dispatch({ selection: { anchor: 0, head: r.view.state.doc.length } });
		expect(hiddenOf(r, A.threadId)).toBe(0);
		expect(hiddenOf(r, B.threadId)).toBe(0);
	});

	it('row 21: scrolling does not end typing', () => {
		const r = make();
		r.edit(A, append(TEXT));
		r.view.scrollDOM.dispatchEvent(new Event('scroll'));
		vi.advanceTimersByTime(500);
		expect(lane(r, A.threadId)).toBeDefined();
		expect(hiddenOf(r, A.threadId)).toBeGreaterThan(0);
	});

	it('row 22: an edit starting offscreen shows at once and moves the caret there', () => {
		const r = make();
		r.options.inView = false;
		r.edit(A, append('far away text'));
		expect(hiddenOf(r, A.threadId)).toBe(0);
		expect(lane(r, A.threadId)?.caret).toBe(r.view.state.doc.length);
	});

	it('row 23: a hidden tab shows everything', () => {
		const r = make();
		r.edit(A, append(TEXT));
		vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
		document.dispatchEvent(new Event('visibilitychange'));
		expect(hiddenOf(r, A.threadId)).toBe(0);
		vi.restoreAllMocks();
	});

	it('row 24: a disconnect (reveal all) shows every span, and an update without author never types', () => {
		const r = make();
		r.edit(A, append(TEXT));
		settleTypedReveals(r.view);
		expect(hiddenOf(r, A.threadId)).toBe(0);
		r.edit(null, append(' resynced'));
		expect(r.lanes().every((l) => hiddenCount(l.segments) === 0)).toBe(true);
	});

	it('row 25: reduced motion means nothing is hidden and nothing types', () => {
		const r = make();
		r.options.canType = false;
		r.edit(A, append(TEXT));
		expect(hiddenOf(r, A.threadId)).toBe(0);
	});

	it('row 28: destroying the editor stops all timers and drops lanes', () => {
		const r = make();
		r.edit(A, append(TEXT));
		r.view.destroy();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('row 28: a document switch (setState) leaves no lanes', () => {
		const r = make();
		r.edit(A, append(TEXT));
		r.view.setState(EditorState.create({ doc: 'other', extensions: [createTypedReveal()] }));
		expect(r.view.state.field(typedReveals)).toHaveLength(0);
	});

	it('row 30: two editors on the same document type independently', () => {
		const r1 = make();
		const r2 = make();
		r1.edit(A, append(TEXT));
		expect(hiddenOf(r1, A.threadId)).toBe(TEXT.length);
		expect(r2.lanes()).toHaveLength(0);
	});
});

