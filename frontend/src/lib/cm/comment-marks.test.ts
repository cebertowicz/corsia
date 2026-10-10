import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { MAX_SCROLL_PASSES, scrollToRange } from './comment-marks';

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
