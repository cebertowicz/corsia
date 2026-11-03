import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorView } from '@codemirror/view';
import { captureDocumentPlace, dispatchKeepingPlace, keepPlaceWhenEditorMoves } from './documentReadingPlace';
import { restoreDocumentPlace } from './documentImagePlace';
import { NEAR_BOTTOM_PX } from './nearBottom';
import readingSource from './documentReadingPlace.ts?raw';
import imageSource from './documentImagePlace.ts?raw';

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement('div');
	document.body.appendChild(root);
});
afterEach(() => {
	const rangeLayout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	delete rangeLayout.getClientRects;
	delete rangeLayout.getBoundingClientRect;
	root.remove();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
const IMAGE = '![Mock screen](shots/screen.png)';
const numbered = (lines: number, image?: number) =>
	Array.from({ length: lines }, (_, i) => (i === image ? IMAGE : `Line ${i}`)).join('\n');

/**
 * A document pane on a model screen. The pane's top edge is at 0; its header is `header` px tall and the editor's scroller
 * fills the rest of its `height`. jsdom has no layout, so CodeMirror keeps its estimated line heights and the model lays
 * the text by them: a line is on screen at the scroller's top, less the offset, plus the line's height-map top. A frame
 * runs as a browser runs one: scroll events, animation-frame callbacks (with the microtasks after each), layout (which
 * clamps the offset to the scroll range), resize observations, paint. `painted` is where the reader's line was on screen,
 * in viewport pixels, at each paint.
 */
function pane({ lines = 400, header = 49, height = 600, image }: { lines?: number; header?: number; height?: number; image?: number } = {}) {
	const frameEl = document.createElement('div');
	frameEl.className = 'doc';
	root.appendChild(frameEl);
	const screen = { header, height, offset: 0 };
	let scrolled = false;
	let queued: FrameRequestCallback[] = [];
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => queued.push(callback));
	vi.stubGlobal('cancelAnimationFrame', () => {});
	// jsdom has no range geometry; CodeMirror reads it to scroll a position into view.
	const rangeLayout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	rangeLayout.getClientRects = () => [] as unknown as DOMRectList;
	rangeLayout.getBoundingClientRect = () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }) as DOMRect;
	const observers: { callback: () => void; targets: Element[] }[] = [];
	vi.stubGlobal('ResizeObserver', class {
		targets: Element[] = [];
		constructor(public callback: () => void) { observers.push(this); }
		observe(el: Element) { this.targets.push(el); }
		disconnect() { observers.splice(observers.indexOf(this), 1); }
	});
	const view = new EditorView({ doc: numbered(lines, image), parent: frameEl, dispatchTransactions: dispatchKeepingPlace });
	const scroller = view.scrollDOM;
	const max = () => Math.max(0, view.contentHeight - (screen.height - screen.header));
	Object.defineProperties(scroller, {
		scrollHeight: { configurable: true, get: () => view.contentHeight },
		clientHeight: { configurable: true, get: () => screen.height - screen.header },
		scrollTop: {
			configurable: true,
			get: () => screen.offset,
			// The browser keeps whole pixels of an offset, inside the scroll range.
			set: (value: number) => { screen.offset = Math.round(Math.min(max(), Math.max(0, value))); scrolled = true; },
		},
	});
	// Zero-sized boxes keep CodeMirror on its estimated heights; only the tops matter here.
	const box = (top: number) => ({ top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
	vi.spyOn(frameEl, 'getBoundingClientRect').mockImplementation(() => box(0));
	vi.spyOn(scroller, 'getBoundingClientRect').mockImplementation(() => box(screen.header));
	vi.spyOn(view.contentDOM, 'getBoundingClientRect').mockImplementation(() => box(screen.header - screen.offset));
	const stop = keepPlaceWhenEditorMoves(view);

	/** Where the line reading `text` starts on screen. */
	const onScreen = (text: string) => {
		const at = view.state.doc.toString().split('\n').indexOf(text);
		return view.documentTop + view.lineBlockAt(view.state.doc.line(at + 1).from).top;
	};
	let reading = '';
	/** Records where the line reading `text` is at each paint from now on. */
	const follow = (text: string) => { reading = text; painted.length = 0; };
	const painted: number[] = [];
	let laidOut = '';
	const size = () => [screen.header, screen.height, view.contentHeight].join();
	laidOut = size();
	/** Runs one frame; `layout` changes the page (a header growing) after the frame's callbacks, as a CSS transition does. */
	const frame = async (layout?: () => void) => {
		if (scrolled) {
			scrolled = false;
			scroller.dispatchEvent(new Event('scroll'));
		}
		const callbacks = queued;
		queued = [];
		for (const callback of callbacks) {
			callback(performance.now());
			await flush();
		}
		layout?.();
		if (screen.offset > max()) {
			screen.offset = max();
			scrolled = true;
		}
		if (size() !== laidOut) {
			laidOut = size();
			for (const observer of [...observers]) observer.callback();
		}
		if (reading) painted.push(onScreen(reading));
	};
	/** The reader scrolls to `offset` and reads the line at the scroller's top edge from then on. */
	const readAt = async (offset: number) => {
		scroller.scrollTop = offset;
		await frame();
		await frame();
		reading = view.state.doc.lineAt(view.lineBlockAtHeight(screen.header - view.documentTop).from).text;
		painted.length = 0;
		return onScreen(reading);
	};
	return { view, scroller, screen, frameEl, observers, painted, frame, readAt, follow, onScreen, max, stop, read: () => reading };
}

/** Painted tops further than `px` from `top`. */
const off = (painted: number[], top: number, px = 1) => painted.filter((at) => Math.abs(at - top) > px);

describe('keepPlaceWhenEditorMoves', () => {
	it('holds the reader\'s line on screen while the review bar eases open mid-document', async () => {
		const page = pane();
		const top = await page.readAt(2000);
		for (let step = 0; step < 10; step++) await page.frame(() => { page.screen.header += 10.4; });
		await page.frame();
		expect(page.painted).toHaveLength(11);
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('holds it while a settling notice comes and goes', async () => {
		const page = pane();
		const top = await page.readAt(1500);
		await page.frame(() => { page.screen.header += 30; });
		await page.frame();
		await page.frame(() => { page.screen.header -= 30; });
		await page.frame();
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('leaves a reader at the very start there, so the title stays in view under a review bar that arrives late', async () => {
		const page = pane();
		const title = await page.readAt(0);
		await page.frame(() => { page.screen.header += 104; });
		await page.frame();
		expect(page.scroller.scrollTop).toBe(0);
		expect(page.painted.at(-1)).toBe(title + 104);
		page.stop();
	});

	for (const fromEnd of [0, 40, 200]) {
		it(`holds a reader ${fromEnd} px from the end when the header grows and shrinks, with the browser clamping the offset`, async () => {
			const page = pane({ header: 157 });
			const top = await page.readAt(page.max() - fromEnd);
			await page.frame(() => { page.screen.header -= 108; });
			await page.frame();
			await page.frame(() => { page.screen.header += 108; });
			await page.frame();
			expect(off(page.painted, top)).toEqual([]);
			page.stop();
		});
	}

	it('observes the scroller and its text through one resize observation', () => {
		const page = pane();
		// CodeMirror observes the scroller too, for its own measuring; only this module observes the text.
		const ours = () => page.observers.filter((observer) => observer.targets.includes(page.view.contentDOM));
		expect(ours().map((observer) => observer.targets)).toEqual([[page.scroller, page.view.contentDOM]]);
		page.stop();
		expect(ours()).toEqual([]);
	});

	it('takes a scroll of the reader\'s as their place, never its own', async () => {
		const page = pane();
		await page.readAt(1000);
		const top = await page.readAt(3000);
		await page.frame(() => { page.screen.header += 60; });
		await page.frame();
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});
});

describe('dispatchKeepingPlace', () => {
	it('holds the reader\'s line through a 30-piece streamed write above it, without creeping', async () => {
		const page = pane();
		const top = await page.readAt(2000);
		for (let piece = 0; piece < 30; piece++) {
			page.view.dispatch({ changes: { from: 0, insert: `Streamed ${piece}\n` } });
			await page.frame();
		}
		expect(page.painted).toHaveLength(30);
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('holds a near-bottom reader in place, never following, for remote edits that are not appends', async () => {
		const page = pane();
		const top = await page.readAt(page.max() - 10);
		for (const change of [
			(end: number) => ({ from: end - 1, insert: '\n' }),
			() => ({ from: 0, insert: 'zero\n' }),
			(end: number) => ({ from: end - 2, to: end }),
		]) {
			page.view.dispatch({ changes: change(page.view.state.doc.length) });
			await page.frame();
		}
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('follows text another writer appends when the reader is within the near-bottom distance of the end', async () => {
		const page = pane();
		await page.readAt(page.max() - (NEAR_BOTTOM_PX - 1));
		page.view.dispatch({ changes: { from: page.view.state.doc.length, insert: '\nfour\nfive' } });
		await page.frame();
		expect(page.scroller.scrollTop).toBe(page.max());
		page.view.dispatch({ changes: { from: page.view.state.doc.length, insert: '\nsix' } });
		await page.frame(() => { page.screen.header += 40; });
		expect(page.scroller.scrollTop).toBe(page.max());
		page.stop();
	});

	it('keeps the place of a reader at or beyond the near-bottom distance when text is appended', async () => {
		const page = pane();
		const top = await page.readAt(page.max() - NEAR_BOTTOM_PX);
		page.view.dispatch({ changes: { from: page.view.state.doc.length, insert: '\nfour' } });
		await page.frame();
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('follows a near-bottom reader when a batch carries an append alongside an edit higher up', async () => {
		const page = pane();
		await page.readAt(page.max() - 10);
		const above = page.view.state.update({ changes: { from: 0, insert: 'zero\n' } });
		const append = above.state.update({ changes: { from: above.state.doc.length, insert: '\nfour' } });
		page.view.dispatch([above, append]);
		await page.frame();
		expect(page.scroller.scrollTop).toBe(page.max());
		page.stop();
	});

	it('lets the editor scroll to the caret or a comment, and holds wherever that lands', async () => {
		const page = pane();
		await page.readAt(1000);
		// CodeMirror scrolls inside its next measure; this model has no layout for it to read, so the write is made there by hand.
		page.view.requestMeasure({ read: () => null, write: () => { page.scroller.scrollTop = 4000; } });
		page.view.dispatch({ effects: EditorView.scrollIntoView(page.view.state.doc.line(300).from, { y: 'start' }) });
		// A notice opens in the same frame: the resize observation comes before the scroll's event. The jump stands, and the
		// text it landed on holds still under the notice.
		await page.frame(() => { page.screen.header += 50; });
		expect(page.scroller.scrollTop).toBe(4050);
		const landed = page.view.state.doc.lineAt(captureDocumentPlace(page.view).from).text;
		page.follow(landed);
		const top = page.onScreen(landed);
		await page.frame(() => { page.screen.header -= 50; });
		await page.frame();
		expect(page.scroller.scrollTop).toBe(4000);
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});
});

describe('dispatchKeepingPlace timing', () => {
	// No frame ever runs, so a measure pass that runs now is seen as the waiting frame being cancelled (CodeMirror's
	// measure cancels it before measuring).
	const FRAME = 7;
	let cancelFrame: ReturnType<typeof vi.fn>;
	beforeEach(() => {
		cancelFrame = vi.fn();
		vi.stubGlobal('requestAnimationFrame', () => FRAME);
		vi.stubGlobal('cancelAnimationFrame', cancelFrame);
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
});

describe('restoreDocumentPlace', () => {
	/** Runs frames until the restore ends; returns how many ran. `layout` may change the page in a given frame. */
	async function run(page: ReturnType<typeof pane>, pending: Promise<void>, layout: (frame: number) => void = () => {}) {
		let done = false;
		void pending.then(() => { done = true; });
		await flush();
		let frames = 0;
		while (!done && frames < 400) await page.frame(() => layout(frames++));
		expect(done).toBe(true);
		return frames;
	}

	/** The reader leaves a place at `offset` for the start; the place then comes back while they read `line`. */
	async function leftAt(page: ReturnType<typeof pane>, offset: number) {
		const top = await page.readAt(offset);
		const line = page.read();
		const place = captureDocumentPlace(page.view);
		await page.readAt(0);
		page.follow(line);
		return { top, place };
	}

	it('holds the line on screen when the header grows during a tab return (the review bar or a notice at frame 2)', async () => {
		const page = pane();
		const { top, place } = await leftAt(page, 3000);
		const placed: number[] = [];
		const pending = restoreDocumentPlace(root, place, { mode: 'tab-return', onPlaced: () => placed.push(page.onScreen(page.read())) });
		const frames = await run(page, pending, (frame) => { if (frame === 2) page.screen.header += 40; });
		expect(frames).toBeGreaterThan(3);
		expect(placed).toEqual([top]);
		expect(page.painted).toHaveLength(frames);
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('holds an image on screen when the header grows during its Back', async () => {
		const page = pane({ image: 220 });
		const line = page.view.state.doc.line(221);
		await page.readAt(page.view.lineBlockAt(line.from).top - 90);
		const top = page.onScreen(IMAGE);
		const request = { src: '/api/files/content?path=shots%2Fscreen.png', alt: 'Mock screen', from: line.from, to: line.to, scrollTop: page.scroller.scrollTop, top };
		await page.readAt(0);
		page.follow(IMAGE);
		const frames = await run(page, restoreDocumentPlace(root, request), (frame) => { if (frame === 2) page.screen.header += 40; });
		expect(page.painted).toHaveLength(frames);
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('falls back to the line that was at the top at the saved offset when an edit moved the image', async () => {
		const page = pane({ image: 220 });
		const line = page.view.state.doc.line(221);
		const at = page.view.lineBlockAt(line.from).top - 90;
		const top = await page.readAt(at);
		const reading = page.read();
		const request = { src: 'x', alt: 'Mock screen', from: line.from + 3, to: line.to + 3, scrollTop: at, top: 0 };
		await page.readAt(0);
		page.follow(reading);
		await run(page, restoreDocumentPlace(root, request));
		expect(page.scroller.scrollTop).toBe(at);
		expect(off(page.painted, top)).toEqual([]);
		page.stop();
	});

	it('does nothing when the document is not on screen', async () => {
		await expect(restoreDocumentPlace(undefined, { from: 0, top: 0, scrollTop: 300 })).resolves.toBeUndefined();
		await expect(restoreDocumentPlace(root, { from: 0, top: 0, scrollTop: 300 })).resolves.toBeUndefined();
	});

	it('ends when the reader scrolls, and never moves the document back over them', async () => {
		const page = pane();
		const { place } = await leftAt(page, 3000);
		const pending = restoreDocumentPlace(root, place, { mode: 'back' });
		await flush();
		await page.frame();
		page.scroller.scrollTop = 5000;
		const frames = await run(page, pending);
		expect(frames).toBeLessThan(3);
		await page.frame(() => { page.screen.header += 40; });
		expect(page.scroller.scrollTop).toBe(5040);
		page.stop();
	});

	it('stops a superseded restore before it touches the document', async () => {
		const page = pane();
		const { place } = await leftAt(page, 3000);
		const controller = new AbortController();
		const placed = vi.fn();
		const pending = restoreDocumentPlace(root, place, { mode: 'tab-return', signal: controller.signal, onPlaced: placed });
		controller.abort();
		await run(page, pending);
		expect(page.scroller.scrollTop).toBe(0);
		expect(placed).not.toHaveBeenCalled();
		page.stop();
	});
});

/** The source with its comments taken out, so prose about a write is not counted as one. */
const code = (source: string) => source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

describe('one place, one writer', () => {
	const both = code(readingSource) + code(imageSource);

	it('writes the scroll offset in exactly one place, applyPlace', () => {
		expect(both.match(/\.scrollTop\s*[-+]?=(?!=)/g)).toHaveLength(1);
		const applyPlace = /export function applyPlace[\s\S]*?\n}\n/.exec(code(readingSource))![0];
		expect(applyPlace).toMatch(/\.scrollTop\s*=(?!=)/);
	});

	it('has one place type and one way to read a place', () => {
		expect(both.match(/interface \w*Place\b/g)).toEqual(['interface TextDocumentPlace']);
		expect(both.match(/function capture\w*/g)).toEqual(['function captureDocumentPlace']);
	});

	it('makes one resize observation per document scroller', () => {
		expect(both.match(/new ResizeObserver/g)).toHaveLength(1);
	});
});
