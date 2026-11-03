import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorView } from '@codemirror/view';
import { captureDocumentPlace, dispatchKeepingPlace, keepPlaceWhenEditorMoves, placeTop } from './documentReadingPlace';
import { NEAR_BOTTOM_PX } from './nearBottom';

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement('div');
	document.body.appendChild(root);
});
afterEach(() => {
	root.remove();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('keepPlaceWhenEditorMoves', () => {
	/** An editor `scrollTop` px down its text, under a header `header` px tall; records scroll writes and runs its one observation. */
	function kept(scrollTop: number) {
		const scroller = document.createElement('div');
		const state = { header: 0, offset: scrollTop };
		const writes: number[] = [];
		Object.defineProperties(scroller, {
			scrollHeight: { configurable: true, get: () => 5000 },
			clientHeight: { configurable: true, get: () => 600 - state.header },
			scrollTop: { configurable: true, get: () => state.offset, set: (value: number) => { state.offset = Math.max(0, value); writes.push(value); } },
		});
		vi.spyOn(scroller, 'getBoundingClientRect').mockImplementation(() => ({ top: state.header }) as DOMRect);
		let observed = () => {};
		vi.stubGlobal('ResizeObserver', class {
			constructor(callback: () => void) { observed = callback; }
			observe() {}
			disconnect() {}
		});
		const stop = keepPlaceWhenEditorMoves({ scrollDOM: scroller, contentDOM: document.createElement('div') } as unknown as EditorView);
		return { state, writes, stop, headerGrows: (px: number) => { state.header += px; observed(); } };
	}

	it('moves the text with the header for a reader at the very start, so a review bar arriving after the editor never scrolls the title away', () => {
		const { state, writes, stop, headerGrows } = kept(0);
		headerGrows(80);
		expect(state.offset).toBe(0);
		expect(writes).toEqual([]);
		stop();
	});

	it('holds the text of a reader further down where it is on screen when the header grows', () => {
		const { state, stop, headerGrows } = kept(300);
		headerGrows(80);
		expect(state.offset).toBe(380);
		stop();
	});
});

describe('dispatchKeepingPlace', () => {
	// jsdom has no layout: CodeMirror's measure needs range geometry. No frame ever runs, so a measure pass that runs now
	// is seen as the waiting frame being cancelled (CodeMirror's measure cancels it before measuring).
	const rangeLayout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	const FRAME = 7;
	let cancelFrame: ReturnType<typeof vi.fn>;
	beforeEach(() => {
		cancelFrame = vi.fn();
		vi.stubGlobal('requestAnimationFrame', () => FRAME);
		vi.stubGlobal('cancelAnimationFrame', cancelFrame);
		rangeLayout.getClientRects = () => [] as unknown as DOMRectList;
		rangeLayout.getBoundingClientRect = () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }) as DOMRect;
	});
	afterEach(() => {
		delete rangeLayout.getClientRects;
		delete rangeLayout.getBoundingClientRect;
	});

	/** An editor with a measure pass waiting for the next frame; `measured` reports whether it has run since. */
	function waiting() {
		const view = new EditorView({ doc: 'one\ntwo\nthree', parent: root, dispatchTransactions: dispatchKeepingPlace });
		view.requestMeasure();
		cancelFrame.mockClear();
		return { view, measured: () => cancelFrame.mock.calls.some(([id]) => id === FRAME) };
	}

	it('lays out a change the reader did not make before any other update can follow it', () => {
		const { view, measured } = waiting();
		view.dispatch({ changes: { from: 0, to: 4 } });
		expect(measured()).toBe(true);
		view.destroy();
	});

	it("leaves the reader's own typing and any update that changes no text to the next frame", () => {
		const { view, measured } = waiting();
		view.dispatch({ changes: { from: 0, insert: 'x' }, userEvent: 'input.type' });
		view.dispatch({ selection: { anchor: 2 } });
		expect(view.state.doc.toString()).toBe('xone\ntwo\nthree');
		expect(measured()).toBe(false);
		view.destroy();
	});

	/**
	 * An editor whose scroller reports a 1000px document in a 400px view, `fromEnd` px above its end, and whose text moves
	 * up the screen as far as it is scrolled; records scroll writes.
	 */
	function reading(fromEnd: number, doc = 'one\ntwo\nthree\n\n') {
		const view = new EditorView({ doc, parent: root, dispatchTransactions: dispatchKeepingPlace });
		const writes: number[] = [];
		let top = 1000 - 400 - fromEnd;
		Object.defineProperties(view.scrollDOM, {
			scrollHeight: { configurable: true, get: () => 1000 },
			clientHeight: { configurable: true, get: () => 400 },
			scrollTop: { configurable: true, get: () => top, set: (value: number) => { top = value; writes.push(value); } },
		});
		vi.spyOn(view.contentDOM, 'getBoundingClientRect').mockImplementation(() => ({ top: -top }) as DOMRect);
		return { view, writes };
	}

	/** Enough distinct lines to fill the reported 1000px. */
	const LONG = Array.from({ length: 100 }, (_, i) => `line ${i}`).join('\n');

	it('follows text another writer appends when the reader is within the near-bottom distance of the end', () => {
		const { view, writes } = reading(NEAR_BOTTOM_PX - 1);
		view.dispatch({ changes: { from: view.state.doc.length, insert: 'four\n' } });
		expect(writes).toEqual([1000]);
		view.destroy();
	});

	it('keeps the place of a reader at or beyond the near-bottom distance when text is appended', () => {
		const { view, writes } = reading(NEAR_BOTTOM_PX, LONG);
		view.dispatch({ changes: { from: view.state.doc.length, insert: 'four\n' } });
		expect(writes).toEqual([]);
		view.destroy();
	});

	it('holds a near-bottom reader in place, never following, for remote edits that are not appends', () => {
		const { view } = reading(10, LONG);
		const place = captureDocumentPlace(view);
		const text = view.state.doc.lineAt(place.from).text;
		/** How far below the scroller's top edge the line the reader had at the top starts now. */
		const readerLine = () => {
			const at = view.state.doc.toString().split('\n').indexOf(text);
			return placeTop(view, view.state.doc.line(at + 1).from);
		};
		expect(text).toMatch(/^line \d+$/);
		for (const change of [
			(end: number) => ({ from: end - 1, insert: '\n' }),
			() => ({ from: 0, insert: 'zero\n' }),
			(end: number) => ({ from: end - 2, to: end }),
		]) {
			view.dispatch({ changes: change(view.state.doc.length) });
			expect(readerLine()).toBeCloseTo(place.top, 0);
		}
		view.destroy();
	});

	it("leaves a near-bottom reader's own append to the editor's default scrolling", () => {
		const { view, writes } = reading(10);
		view.dispatch({ changes: { from: view.state.doc.length, insert: 'x' }, userEvent: 'input.type' });
		expect(writes).toEqual([]);
		view.destroy();
	});

	it('follows a near-bottom reader when a batch carries an append alongside an edit higher up', () => {
		const { view, writes } = reading(10);
		const above = view.state.update({ changes: { from: 0, insert: 'zero\n' } });
		const append = above.state.update({ changes: { from: above.state.doc.length, insert: 'four\n' } });
		view.dispatch([above, append]);
		expect(view.state.doc.toString()).toBe('zero\none\ntwo\nthree\n\nfour\n');
		expect(writes).toEqual([1000]);
		view.destroy();
	});
});
