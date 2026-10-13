import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorView } from '@codemirror/view';
import { restoreDocumentPlace } from './documentImagePlace';

const SRC = '/api/files/content?path=%2Fws%2Fa.png';
const image = { instanceId: 'tab-1', src: SRC, alt: 'a', from: 40, to: 60, scrollTop: 300, top: 0 };
let root: HTMLElement;
let scrollIntoView: ReturnType<typeof vi.fn>;

function frames() {
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(0), 0));
}

beforeEach(() => {
	frames();
	scrollIntoView = vi.fn();
	Element.prototype.scrollIntoView = scrollIntoView as unknown as typeof Element.prototype.scrollIntoView;
	root = document.createElement('div');
	root.innerHTML = `<div class="cm-scroller"><div class="cm-md-image" data-from="40" data-src="${SRC}"></div><div class="cm-md-image" data-from="90" data-src="${SRC}"></div></div>`;
	document.body.appendChild(root);
});
afterEach(() => {
	root.remove();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

/** An editor whose measure pass reads and writes in a microtask, and whose layout is the page's own. */
function editor(scroller: HTMLElement, extra: Record<string, unknown> = {}) {
	return vi.spyOn(EditorView, 'findFromDOM').mockReturnValue({
		state: { doc: { length: 1000 } },
		viewport: { from: 0, to: 0 },
		get documentTop() { return -scroller.scrollTop; },
		lineBlockAt: () => ({ top: 0 }),
		requestMeasure: ({ read, write }: { read: () => number; write: (top: number) => void }) => {
			queueMicrotask(() => write(read()));
		},
		...extra,
	} as unknown as EditorView);
}

describe('restoreDocumentPlace', () => {
	it('puts the scroll position back and never scrolls the image into view over it', async () => {
		await restoreDocumentPlace(root, image);
		expect(root.querySelector('.cm-scroller')!.scrollTop).toBe(300);
		expect(scrollIntoView).not.toHaveBeenCalled();
	});

	it('falls back to the scroll position alone when an edit moved the image', async () => {
		await restoreDocumentPlace(root, { ...image, from: 41 });
		expect(root.querySelector('.cm-scroller')!.scrollTop).toBe(300);
		expect(scrollIntoView).not.toHaveBeenCalled();
	});

	it('does nothing when the document is not on screen', async () => {
		await expect(restoreDocumentPlace(undefined, image)).resolves.toBeUndefined();
		root.replaceChildren();
		await expect(restoreDocumentPlace(root, image)).resolves.toBeUndefined();
		expect(scrollIntoView).not.toHaveBeenCalled();
	});

	it('waits for the layout after the viewer closes before reading anything', async () => {
		const pending = restoreDocumentPlace(root, image);
		expect(root.querySelector('.cm-scroller')!.scrollTop).toBe(0);
		await pending;
		expect(root.querySelector('.cm-scroller')!.scrollTop).toBe(300);
	});

	it('holds the image where it was while the text re-measures after the scroll position is set', async () => {
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const widget = root.querySelector<HTMLElement>('[data-from="40"]')!;
		// The text above the image measures 90 px taller once it is drawn, whatever the offset that was saved.
		vi.spyOn(widget, 'getBoundingClientRect').mockImplementation(() => ({ top: 490 - scroller.scrollTop }) as DOMRect);
		await restoreDocumentPlace(root, { ...image, top: 100 });
		expect(scroller.scrollTop).toBe(390);
	});

	it('holds the first visible text line while CodeMirror re-measures after a tab returns', async () => {
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		editor(scroller, { coordsAtPos: () => ({ top: 490 - scroller.scrollTop }) });

		await restoreDocumentPlace(root, { from: 80, top: 100, scrollTop: 300 });

		expect(scroller.scrollTop).toBe(390);
	});

	it('keeps holding the image while the text goes on re-measuring, and lets go at the first touch of the user', async () => {
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const widget = root.querySelector<HTMLElement>('[data-from="40"]')!;
		let shift = 0;
		vi.spyOn(widget, 'getBoundingClientRect').mockImplementation(() => ({ top: 100 + shift - (scroller.scrollTop - 300) }) as DOMRect);
		const pending = restoreDocumentPlace(root, { ...image, top: 100 });
		await new Promise((resolve) => setTimeout(resolve, 20));
		shift = 24;
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(scroller.scrollTop).toBe(324);
		scroller.dispatchEvent(new Event('wheel'));
		shift = 60;
		await pending;
		expect(scroller.scrollTop).toBe(324);
	});

	it('holds the image in place on every painted frame of Back while the pane eases, even when a rewrap lands after the animation frame', async () => {
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const widget = root.querySelector<HTMLElement>('[data-from="40"]')!;
		editor(scroller);
		// The canvas narrows by 90 px a frame for six frames; the rewrap that follows each step, laid out after the
		// frame's animation callbacks, pushes the image 30 px further down.
		let width = 900;
		let rewrap = 0;
		vi.spyOn(root, 'getBoundingClientRect').mockImplementation(() => ({ width }) as DOMRect);
		vi.spyOn(widget, 'getBoundingClientRect').mockImplementation(() => ({ top: 124 + rewrap - (scroller.scrollTop - 300) }) as DOMRect);
		let observed: (() => void) | null = null;
		vi.stubGlobal('ResizeObserver', class {
			constructor(callback: () => void) { observed = callback; }
			observe() {}
			disconnect() { observed = null; }
		});
		let queued: FrameRequestCallback[] = [];
		const painted: number[] = [];
		let frame = 0;
		const runFrame = async () => {
			const callbacks = queued;
			queued = [];
			const easing = frame++ < 6;
			if (easing) width -= 90;
			for (const callback of callbacks) callback(0);
			await new Promise((resolve) => setTimeout(resolve, 0));
			// Layout, then resize observations, then paint.
			if (easing) rewrap += 30;
			if (easing || frame === 1) (observed as (() => void) | null)?.();
			painted.push(widget.getBoundingClientRect().top);
			if (queued.length) setTimeout(runFrame, 0);
		};
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
			if (!queued.length) setTimeout(runFrame, 0);
			queued.push(callback);
			return 0;
		});

		await restoreDocumentPlace(root, { ...image, top: 100 });

		expect(painted.length).toBeGreaterThan(6);
		expect(painted.filter((top) => top !== 100)).toEqual([]);
		expect(scroller.scrollTop).toBe(504);
	});

	it('applies a tab return place without waiting for the pane width to settle', async () => {
		let width = 900;
		vi.spyOn(root, 'getBoundingClientRect').mockImplementation(() => ({ width }) as DOMRect);
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const placed = vi.fn();
		const pending = restoreDocumentPlace(root, image, { mode: 'tab-return', onPlaced: placed });
		await new Promise((resolve) => setTimeout(resolve, 10));
		width -= 90;
		expect(scroller.scrollTop).toBe(300);
		expect(placed).toHaveBeenCalledOnce();
		await pending;
	});

	it('hands a returning text line to the editor as its scroll target and reveals the tab once the measure finds it there', async () => {
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const readAt: number[] = [];
		const target = vi.spyOn(EditorView, 'scrollIntoView');
		const found = vi.spyOn(EditorView, 'findFromDOM').mockReturnValue({
			state: { doc: { length: 1000 } },
			viewport: { from: 0, to: 0 },
			get documentTop() { return -scroller.scrollTop; },
			lineBlockAt: () => ({ top: 400 }),
			coordsAtPos: () => (scroller.scrollTop < 200 ? null : { top: 490 - scroller.scrollTop }),
			// The editor lays its scroll target from the line as it draws it, inside its own measure pass.
			dispatch: () => { scroller.scrollTop = 390; },
			requestMeasure: ({ read, write }: { read: () => number; write: (top: number) => void }) => {
				queueMicrotask(() => { readAt.push(scroller.scrollTop); write(read()); });
			},
		} as unknown as EditorView);
		const placedAt: number[] = [];
		await restoreDocumentPlace(root, { from: 80, top: 100, scrollTop: 2000 }, {
			mode: 'tab-return',
			onPlaced: () => placedAt.push(scroller.scrollTop),
		});
		// The saved offset was never used, and no offset was written over the editor's own placement: every measured pass
		// found the line already where it was.
		expect(target).toHaveBeenCalledWith(80, { y: 'start', yMargin: 100 });
		expect(new Set(readAt)).toEqual(new Set([390]));
		expect(placedAt).toEqual([390]);
		expect(scroller.scrollTop).toBe(390);
		found.mockRestore();
	});

	it('stops a superseded text restore inside the measure pass, before it moves the document again', async () => {
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const controller = new AbortController();
		let passes = 0;
		editor(scroller, {
			coordsAtPos: () => ({ top: 490 - scroller.scrollTop }),
			dispatch: () => {},
			// A newer restore starts while this one's pass is pending.
			requestMeasure: ({ read, write }: { read: () => number; write: (top: number) => void }) => {
				passes += 1;
				controller.abort();
				write(read());
			},
		});
		const placed = vi.fn();
		await restoreDocumentPlace(root, { from: 80, top: 100, scrollTop: 2000 }, { mode: 'tab-return', signal: controller.signal, onPlaced: placed });
		expect(scroller.scrollTop).toBe(0);
		expect(passes).toBe(1);
		expect(placed).not.toHaveBeenCalled();
	});

	it('stops a superseded anchor correction before it can overwrite a newer restore', async () => {
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const widget = root.querySelector<HTMLElement>('[data-from="40"]')!;
		let shift = 0;
		vi.spyOn(widget, 'getBoundingClientRect').mockImplementation(() => ({ top: 100 + shift - (scroller.scrollTop - 300) }) as DOMRect);
		const controller = new AbortController();
		const pending = restoreDocumentPlace(root, { ...image, top: 100 }, { mode: 'tab-return', signal: controller.signal });
		await new Promise((resolve) => setTimeout(resolve, 10));
		controller.abort();
		shift = 100;
		scroller.scrollTop = 700;
		await pending;
		expect(scroller.scrollTop).toBe(700);
	});
});
