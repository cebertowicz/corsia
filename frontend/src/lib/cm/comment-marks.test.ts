import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { highlightAtPress, MAX_SCROLL_PASSES, scrollToRange, type CommentRange } from './comment-marks';
import { widgetTarget } from './widget-scroll';

vi.mock('./widget-scroll', () => ({ widgetTarget: vi.fn(() => null) }));
// The reading place is the editor's own; these stand-in editors have no line blocks to take it from.
vi.mock('$lib/documentReadingPlace', () => ({ releasePlace: vi.fn() }));

type MeasureRequest = NonNullable<Parameters<EditorView['requestMeasure']>[0]>;

afterEach(() => {
	vi.restoreAllMocks();
	document.body.replaceChildren();
});

// A stand-in editor. `onScreen(jumps, checks)` says whether the target is on
// screen at a check, the way revealed lines settle after a jump lands.
function editorWithTarget(onScreen: (jumps: number, checks: number) => boolean) {
	const scrollDOM = document.createElement('div');
	scrollDOM.style.overflowY = 'auto';
	vi.spyOn(scrollDOM, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 700 } as DOMRect);
	const measures: MeasureRequest[] = [];
	const frames: (() => void)[] = [];
	vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
		frames.push(() => callback(0));
		return frames.length;
	});
	let jumps = 0, checks = 0, measuring = false;
	const dom = document.body.appendChild(document.createElement('div'));
	const view = {
		state: EditorState.create({ doc: 'text\n'.repeat(100) }), scrollDOM, dom,
		// Like CodeMirror, refuse an update during the measure cycle.
		dispatch: vi.fn(() => {
			if (measuring) throw new Error('Calls to EditorView.update are not allowed while an update is in progress');
			jumps += 1;
		}),
		// These navigation cases begin off screen; checks count measurements after a jump.
		coordsAtPos: vi.fn(() => (jumps > 0 && onScreen(jumps, checks++) ? { top: 390, bottom: 410 } : { top: 1400, bottom: 1420 })),
		requestMeasure: (measure: MeasureRequest) => measures.push(measure),
	} as unknown as EditorView;
	// Run the frames and CodeMirror measure cycles it schedules until nothing is pending.
	const settle = () => {
		for (let guard = 0; guard < 100 && (measures.length || frames.length); guard += 1) {
			frames.shift()?.();
			const measure = measures.shift();
			if (!measure) continue;
			measuring = true;
			try { measure.write?.(measure.read(view), view); }
			finally { measuring = false; }
		}
	};
	return { view, dom, measures, frames, settle };
}

describe('document range navigation', () => {
	it('jumps again until the target stays on screen', () => {
		const { view, measures, frames, settle } = editorWithTarget((jumps) => jumps >= 3);
		scrollToRange(view, 450, 454);
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(3);
		expect(measures).toHaveLength(0);
		expect(frames).toHaveLength(0);
	});

	it('jumps again when the target drifts off after first landing on screen', () => {
		// Lands on screen, then revealed lines grow and push it off; the second jump holds.
		const { view, settle } = editorWithTarget((jumps, checks) => jumps >= 2 || checks === 0);
		scrollToRange(view, 450, 454);
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(2);
	});

	it('stops after a bounded number of passes when the target never lands', () => {
		const { view, settle } = editorWithTarget(() => false);
		scrollToRange(view, 450, 454);
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(MAX_SCROLL_PASSES + 1);
	});

	it('stops once the editor leaves the page', () => {
		const { view, dom, settle } = editorWithTarget(() => false);
		scrollToRange(view, 450, 454);
		dom.remove();
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(1);
	});
});

function rect(top: number, bottom: number) {
	return { top, bottom } as DOMRect;
}

// Mirrors the document pane: the pane scrolls, while the editor's own
// scroller grows to the full content height and so never clips anything.
function editorWithStart(start: { top: number; bottom: number } | null) {
	const pane = document.createElement('div');
	pane.style.overflowY = 'auto';
	vi.spyOn(pane, 'getBoundingClientRect').mockReturnValue(rect(100, 700));
	const scrollDOM = document.createElement('div');
	scrollDOM.style.overflowY = 'auto';
	vi.spyOn(scrollDOM, 'getBoundingClientRect').mockReturnValue(rect(-4000, 5000));
	pane.append(scrollDOM);
	document.body.append(pane);
	vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(0);
	return {
		state: EditorState.create({ doc: 'text\n'.repeat(100) }),
		scrollDOM,
		dom: scrollDOM,
		coordsAtPos: vi.fn(() => start),
		dispatch: vi.fn(),
		requestMeasure: vi.fn(),
	} as unknown as EditorView;
}

describe('visible document range targeting', () => {
	it('leaves the viewport alone for a visible range after saving a comment', () => {
		const view = editorWithStart({ top: 220, bottom: 244 });

		scrollToRange(view, 450, 454, 'comment-save');

		expect(view.dispatch).not.toHaveBeenCalled();
	});

	it('centers an explicit navigation target even when it is already visible in the pane', () => {
		const view = editorWithStart({ top: 220, bottom: 244 });

		scrollToRange(view, 450, 454, 'navigation');

		expect(view.dispatch).toHaveBeenCalledWith({ effects: expect.anything() });
	});

	it('centers a rendered range that the scrolling pane clips out of view', () => {
		const view = editorWithStart({ top: 1400, bottom: 1424 });

		scrollToRange(view, 450, 454);

		expect(view.dispatch).toHaveBeenCalledWith({ effects: expect.anything() });
	});

	it('centers a range whose start is only partly visible at the pane edge', () => {
		const view = editorWithStart({ top: 90, bottom: 114 });

		scrollToRange(view, 450, 454);

		expect(view.dispatch).toHaveBeenCalledOnce();
	});

	it('centers a range outside the rendered editor viewport', () => {
		const view = editorWithStart(null);

		scrollToRange(view, 450, 454);

		expect(view.dispatch).toHaveBeenCalledOnce();
	});
});

describe('a jump whose frame moves after it lands', () => {
	// Opening the comments drawer takes width from the pane, which rewraps every line above the landing. The reveal's
	// frame checks have stopped by then, so the landing itself has to be taken again from the pane's own resize.
	function watching() {
		const observers: Array<{ trigger: () => void; disconnected: boolean }> = [];
		vi.stubGlobal('ResizeObserver', class {
			private index = 0;
			constructor(callback: () => void) { this.index = observers.push({ trigger: callback, disconnected: false }) - 1; }
			observe() {}
			disconnect() { observers[this.index].disconnected = true; }
		});
		return observers;
	}

	afterEach(() => vi.unstubAllGlobals());

	it('takes the place again when the pane moves it out of view, and stops watching on the reader scroll', () => {
		const observers = watching();
		let moved = false;
		const { view, settle } = editorWithTarget((jumps) => (moved ? jumps >= 3 : jumps >= 1));

		scrollToRange(view, 450, 454);
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(1);
		const watch = observers.at(-1)!;
		// Its first callback reports the size the box already has, which is not a move.
		watch.trigger();
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(1);

		moved = true;
		watch.trigger();
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(3);

		// The reader then scrolls: their place is theirs, and a later frame move must not pull them back.
		view.scrollDOM.dispatchEvent(new Event('scroll'));
		const before = vi.mocked(view.dispatch).mock.calls.length;
		watch.trigger();
		settle();
		expect(view.dispatch).toHaveBeenCalledTimes(before);
		expect(watch.disconnected).toBe(true);
	});

	it('never moves the reader from a check the pane queued before the reader scrolled', () => {
		const observers = watching();
		let moved = false;
		const { view, settle } = editorWithTarget((jumps) => (moved ? jumps >= 3 : jumps >= 1));

		scrollToRange(view, 450, 454);
		settle();
		const watch = observers.at(-1)!;
		watch.trigger();
		moved = true;
		// The pane moves and queues a check; the reader scrolls before its frame runs.
		watch.trigger();
		view.scrollDOM.dispatchEvent(new Event('scroll'));
		settle();

		expect(view.dispatch).toHaveBeenCalledTimes(1);
	});

	it('never moves the reader from a table row placement queued before the reader jumped elsewhere', () => {
		watching();
		const widget = { start: 440, box: () => ({ top: 1400, bottom: 1420 }) as DOMRect, reveal: vi.fn() };
		vi.mocked(widgetTarget).mockReturnValueOnce(widget);
		const { view, settle } = editorWithTarget((jumps) => jumps >= 1);

		scrollToRange(view, 450, 454);
		scrollToRange(view, 900, 904);
		settle();

		expect(widget.reveal).not.toHaveBeenCalled();
		expect(view.scrollDOM.scrollTop).toBe(0);
	});

	it('retires the watch of the jump the reader replaced', () => {
		const observers = watching();
		const { view, settle } = editorWithTarget((jumps) => jumps >= 1);

		scrollToRange(view, 450, 454);
		settle();
		scrollToRange(view, 900, 904);
		settle();

		expect(observers).toHaveLength(2);
		expect(observers[0].disconnected).toBe(true);
		expect(observers[1].disconnected).toBe(false);
	});
});

describe('the highlight a press is in', () => {
	// A stand-in editor that puts every press at `at` and knows no line edges, so the press counts wherever it is inside a range.
	const pressAt = (at: number, ranges: CommentRange[]) => highlightAtPress({ posAtCoords: () => at, coordsAtPos: () => null } as unknown as EditorView, { clientX: 0, clientY: 0 }, ranges);
	const suggestion = { quote: 'the reviewer signs', insert: 'a reviewer signs', color: 'red', inline: true };

	it('is the one-word comment inside a longer suggestion, not the suggestion that starts before it', () => {
		const ranges: CommentRange[] = [{ id: 'sugg', from: 3, to: 21, suggestion }, { id: 'word', from: 7, to: 15 }];
		expect(pressAt(10, ranges)?.id).toBe('word');
		expect(pressAt(4, ranges)?.id).toBe('sugg');
	});

	it('is the earlier of two highlights covering the same text', () => {
		expect(pressAt(5, [{ id: 'first', from: 3, to: 9 }, { id: 'second', from: 3, to: 9 }])?.id).toBe('first');
	});

	it('is nothing when the press is in no highlight, or only in a point', () => {
		expect(pressAt(30, [{ id: 'a', from: 3, to: 9 }])).toBeUndefined();
		expect(pressAt(5, [{ id: 'p', from: 5, to: 5, point: true }])).toBeUndefined();
	});
});
