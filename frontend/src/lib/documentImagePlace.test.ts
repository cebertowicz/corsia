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
});

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
		const found = vi.spyOn(EditorView, 'findFromDOM').mockReturnValue({
			coordsAtPos: () => ({ top: 490 - scroller.scrollTop }),
		} as unknown as EditorView);

		await restoreDocumentPlace(root, { from: 80, top: 100, scrollTop: 300 });

		expect(scroller.scrollTop).toBe(390);
		found.mockRestore();
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

	it('sets the scroll position only once the width has stopped changing', async () => {
		let width = 900;
		const sizes = vi.spyOn(root, 'getBoundingClientRect').mockImplementation(() => ({ width }) as DOMRect);
		const scroller = root.querySelector<HTMLElement>('.cm-scroller')!;
		const pending = restoreDocumentPlace(root, image);
		for (let step = 0; step < 6; step++) {
			await new Promise((resolve) => setTimeout(resolve, 0));
			width -= 90;
			expect(scroller.scrollTop).toBe(0);
		}
		await pending;
		expect(scroller.scrollTop).toBe(300);
		sizes.mockRestore();
	});
});
