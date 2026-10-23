import { describe, it, expect, afterEach, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { forceParsing } from '@codemirror/language';
import { GFM } from '@lezer/markdown';
import { cursorCharLeft, cursorCharRight, deleteCharBackward, deleteCharForward } from '@codemirror/commands';
import { commentMarks, setCommentRanges } from './comment-marks';
import { commentRail } from './comment-rail';
import { markdownLivePreview } from './markdown-live';
import {
	FAILED_IMAGE, imageContext, imageOpener, imageRequestAt, MISSING_IMAGE, NOT_AN_IMAGE, resolveImagePath, UNAVAILABLE_IMAGE,
	type ImageContext, type OpenImageRequest,
} from './markdown-image';

const DOC_PATH = '/ws/notes/plan.md';
const CONTEXT: ImageContext = { docPath: DOC_PATH, projectId: 'p1', token: 'tok' };
const views: EditorView[] = [];

function mount(doc: string, cursor?: number, context: ImageContext | null = CONTEXT, opener?: (request: OpenImageRequest) => void): EditorView {
	const state = EditorState.create({
		doc,
		selection: cursor != null ? { anchor: cursor } : undefined,
		extensions: [
			markdown({ extensions: GFM }), markdownLivePreview, commentMarks, commentRail, imageContext.of(() => context),
			...(opener ? [imageOpener.of(opener)] : []),
		],
	});
	const view = new EditorView({ state, parent: document.body });
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	views.push(view);
	return view;
}

afterEach(() => {
	while (views.length) views.pop()!.destroy();
	vi.unstubAllGlobals();
});

const imagePath = (img: HTMLImageElement) => new URL(img.src, 'http://x').searchParams.get('path');
const imageCacheKey = (img: HTMLImageElement) => new URL(img.src, 'http://x').searchParams.get('v');
const IMAGE = '![Mock screen](shots/a.png)';
const DOC = `# Plan\n\n${IMAGE}\n\nafter the image\n`;

describe('resolveImagePath', () => {
	it('joins a relative path to the document directory', () => {
		expect(resolveImagePath(DOC_PATH, 'shots/a.png')).toBe('/ws/notes/shots/a.png');
		expect(resolveImagePath(DOC_PATH, './a.png')).toBe('/ws/notes/a.png');
	});
	it('folds .. against the document directory and stops at the root', () => {
		expect(resolveImagePath(DOC_PATH, '../shots/a.png')).toBe('/ws/shots/a.png');
		expect(resolveImagePath(DOC_PATH, '../../../../a.png')).toBe('/a.png');
	});
	it('keeps an absolute path as written', () => {
		expect(resolveImagePath(DOC_PATH, '/ws/other/b.png')).toBe('/ws/other/b.png');
	});
	it('decodes the path and unwraps an angle-bracketed one', () => {
		expect(resolveImagePath(DOC_PATH, 'my%20shots/a.png')).toBe('/ws/notes/my shots/a.png');
		expect(resolveImagePath(DOC_PATH, '<my shots/a.png>')).toBe('/ws/notes/my shots/a.png');
	});
	it('is not a path for a web URL, a data URI, any scheme or a protocol-relative URL', () => {
		for (const url of ['https://example.com/a.png', 'http://example.com/a.png', 'data:image/png;base64,AAAA', 'ftp://h/a.png', '//cdn.example.com/a.png']) {
			expect(resolveImagePath(DOC_PATH, url), url).toBeNull();
		}
	});
	it('has no base for a relative path when the document has no path', () => {
		expect(resolveImagePath(null, 'a.png')).toBeNull();
		expect(resolveImagePath(null, '/ws/a.png')).toBe('/ws/a.png');
	});
});

describe('markdown images in the live preview', () => {
	it('uses one cache key throughout an open document and a new key after reopening it', () => {
		const firstOpen: ImageContext = { ...CONTEXT, imageCacheKey: 'open-1' };
		const sameOpen: ImageContext = { ...CONTEXT, imageCacheKey: 'open-1' };
		const secondOpen: ImageContext = { ...CONTEXT, imageCacheKey: 'open-2' };
		const first = mount(`${IMAGE}\n\n| Image |\n| - |\n| ${IMAGE} |\n`, 0, firstOpen);
		const reopened = mount(`${IMAGE}\n\n| Image |\n| - |\n| ${IMAGE} |\n`, 0, secondOpen);
		const rebuilt = mount(`${IMAGE}\n\n| Image |\n| - |\n| ${IMAGE} |\n`, 0, sameOpen);

		for (const selector of ['.cm-md-image img', '.cm-md-table td img']) {
			expect(imageCacheKey(first.dom.querySelector<HTMLImageElement>(selector)!)).toBe('open-1');
			expect(imageCacheKey(rebuilt.dom.querySelector<HTMLImageElement>(selector)!)).toBe('open-1');
			expect(imageCacheKey(reopened.dom.querySelector<HTMLImageElement>(selector)!)).toBe('open-2');
		}
	});

	it('draws a relative image below its line, named by its alt text, from the content route', () => {
		const view = mount(DOC);
		const img = view.dom.querySelector<HTMLImageElement>('.cm-md-image img')!;
		expect(img.alt).toBe('Mock screen');
		expect(imagePath(img)).toBe('/ws/notes/shots/a.png');
		const params = new URL(img.src, 'http://x').searchParams;
		expect([params.get('raw'), params.get('project_id')]).toEqual(['true', 'p1']);
		expect(img.src).toContain('/api/files/content');
	});

	it('never shows the source, whatever the caret or selection does', () => {
		const at = DOC.indexOf(IMAGE);
		for (const selection of [{ anchor: 0 }, { anchor: at }, { anchor: at + 3 }, { anchor: at + IMAGE.length }, { anchor: 0, head: DOC.length }]) {
			const view = mount(DOC);
			view.dispatch({ selection });
			expect(view.contentDOM.textContent, JSON.stringify(selection)).not.toContain('![Mock screen]');
			expect(view.contentDOM.textContent).not.toContain('shots/a.png');
			expect(view.dom.querySelector('.cm-md-image img')).not.toBeNull();
		}
	});

	it('keeps the source hidden when an edit lands in the image line and when a comment is focused on it', () => {
		const view = mount(DOC, 0);
		const at = DOC.indexOf(IMAGE);
		view.dispatch({ changes: { from: at, insert: 'typed ' } });
		expect(view.contentDOM.textContent).not.toContain('![Mock screen]');
		view.dispatch({ effects: setCommentRanges.of([{ from: at + 6, to: at + 6 + IMAGE.length, id: 'c1', focused: true }]), selection: { anchor: at + 6 } });
		expect(view.contentDOM.textContent).not.toContain('![Mock screen]');
		expect(view.dom.querySelector('.cm-md-image img')).not.toBeNull();
	});

	it('keeps the same image element across a selection move, so it does not reload', () => {
		const view = mount(DOC, 0);
		const before = view.dom.querySelector('.cm-md-image img');
		view.dispatch({ selection: { anchor: DOC.indexOf(IMAGE), head: DOC.indexOf(IMAGE) + IMAGE.length } });
		expect(view.dom.querySelector('.cm-md-image img')).toBe(before);
		expect(view.dom.querySelector('.cm-md-image-selected')).not.toBeNull();
	});

	it('leaves no empty text line above an image that is alone on its line, and keeps the text of an inline one', () => {
		const alone = mount(DOC, 0);
		expect([...alone.contentDOM.querySelectorAll('.cm-line')].map((l) => l.textContent)).toEqual(['Plan', '', '', 'after the image', '']);
		const inline = mount('before ![one](a.png) after\n', 0);
		expect(inline.contentDOM.textContent).toContain('before ');
		expect(inline.contentDOM.textContent).toContain(' after');
		expect(inline.contentDOM.textContent).not.toContain('![one]');
		inline.dispatch({ changes: { from: 0, insert: 'X' } });
		expect(inline.contentDOM.textContent).toContain('Xbefore ');
	});

	it('is one unit for the caret: it steps over the image, and Backspace or Delete next to it removes it whole', () => {
		const at = DOC.indexOf(IMAGE);
		const afterLine = at + IMAGE.length + 1;
		const back = mount(DOC, afterLine);
		cursorCharLeft(back);
		expect(back.state.selection.main.head).toBe(at);
		const alone = mount(DOC, afterLine);
		deleteCharBackward(alone);
		expect(alone.state.doc.toString()).toBe(DOC.replace(`${IMAGE}\n`, ''));

		const inline = 'a ![one](a.png) b\n';
		const [from, to] = [inline.indexOf('!'), inline.indexOf(')') + 1];
		const right = mount(inline, to);
		cursorCharLeft(right);
		expect(right.state.selection.main.head).toBe(from);
		cursorCharRight(right);
		expect(right.state.selection.main.head).toBe(to);
		const bs = mount(inline, to);
		deleteCharBackward(bs);
		expect(bs.state.doc.toString()).toBe('a  b\n');
		const del = mount(inline, from);
		deleteCharForward(del);
		expect(del.state.doc.toString()).toBe('a  b\n');
	});

	it('selects the whole image on a click and marks it selected, so a comment can anchor to its markdown', () => {
		const view = mount(DOC, 0);
		const at = DOC.indexOf(IMAGE);
		view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }));
		const { from, to } = view.state.selection.main;
		expect([from, to]).toEqual([at, at + IMAGE.length]);
		expect(view.dom.querySelector('.cm-md-image-selected img')).not.toBeNull();
		expect(view.contentDOM.textContent).not.toContain('![Mock screen]');
		view.dispatch({ selection: { anchor: 0 } });
		expect(view.dom.querySelector('.cm-md-image-selected')).toBeNull();
	});

	describe('opening an image larger', () => {
		const drawn = (view: EditorView) => {
			const img = view.dom.querySelector<HTMLImageElement>('.cm-md-image img')!;
			Object.defineProperty(img, 'complete', { value: true });
			Object.defineProperty(img, 'naturalWidth', { value: 640 });
			return img;
		};
		const click = (target: Element) => {
			const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 });
			target.dispatchEvent(down);
			target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
			return down;
		};
		const enter = (view: EditorView) => view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));

		it('opens a drawn image on a click and leaves the caret and selection where they were', () => {
			const open = vi.fn();
			const view = mount(DOC, 0, CONTEXT, open);
			const at = DOC.indexOf(IMAGE);
			const down = click(drawn(view));
			expect(down.defaultPrevented).toBe(true);
			expect(open).toHaveBeenCalledTimes(1);
			const request: OpenImageRequest = open.mock.calls[0][0];
			expect(request).toMatchObject({ alt: 'Mock screen', from: at, to: at + IMAGE.length, scrollTop: 0, top: 0 });
			expect(imagePath(Object.assign(new Image(), { src: request.src }))).toBe('/ws/notes/shots/a.png');
			expect(view.state.selection.main.empty).toBe(true);
			expect(view.dom.querySelector('.cm-md-image-selected')).toBeNull();
		});

		it('does not open an image that has not drawn, and a click selects it as before', () => {
			const open = vi.fn();
			const view = mount(DOC, 0, CONTEXT, open);
			click(view.dom.querySelector('.cm-md-image img')!);
			expect(open).not.toHaveBeenCalled();
			expect(view.dom.querySelector('.cm-md-image-selected img')).not.toBeNull();
		});

		it('does not open a failed image, which keeps its note and can still be selected', async () => {
			vi.stubGlobal('fetch', vi.fn(async () => ({ status: 404, body: null })));
			const open = vi.fn();
			const view = mount(DOC, 0, CONTEXT, open);
			view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new Event('error'));
			await vi.waitFor(() => expect(view.dom.querySelector('.cm-md-image-note')).not.toBeNull());
			click(view.dom.querySelector('.cm-md-image-note')!);
			expect(open).not.toHaveBeenCalled();
			expect(view.dom.querySelector('.cm-md-image-selected')).not.toBeNull();
		});

		it('opens the image that is the whole selection on Enter, and leaves Enter alone otherwise', () => {
			const open = vi.fn();
			const view = mount(DOC, 0, CONTEXT, open);
			const at = DOC.indexOf(IMAGE);
			drawn(view);

			enter(view);
			expect(open).not.toHaveBeenCalled();

			view.dispatch({ selection: { anchor: at, head: at + IMAGE.length } });
			enter(view);
			expect(open).toHaveBeenCalledTimes(1);
			expect(open.mock.calls[0][0]).toMatchObject({ from: at, to: at + IMAGE.length });
			expect(view.state.doc.toString()).toBe(DOC);
		});

		it('does nothing on Enter for a selected image that has not drawn', () => {
			const open = vi.fn();
			const view = mount(DOC, 0, CONTEXT, open);
			const at = DOC.indexOf(IMAGE);
			view.dispatch({ selection: { anchor: at, head: at + IMAGE.length } });
			enter(view);
			expect(open).not.toHaveBeenCalled();
		});

		it('gives what opening the image a comment is on needs, once it draws, and leaves the opening to the caller', async () => {
			const open = vi.fn();
			const view = mount(DOC, 0, CONTEXT, open);
			const at = DOC.indexOf(IMAGE);
			const img = view.dom.querySelector<HTMLImageElement>('.cm-md-image img')!;
			Object.defineProperty(img, 'complete', { configurable: true, value: false });
			const request = imageRequestAt(document.body, { from: at, to: at + IMAGE.length });
			await new Promise((resolve) => setTimeout(resolve, 50));
			Object.defineProperty(img, 'complete', { configurable: true, value: true });
			Object.defineProperty(img, 'naturalWidth', { value: 640 });
			img.dispatchEvent(new Event('load'));
			expect(await request).toMatchObject({ alt: 'Mock screen', from: at, to: at + IMAGE.length });
			expect(open).not.toHaveBeenCalled();
		});

		it('gives nothing for a range with no image, or an image that does not draw', async () => {
			vi.stubGlobal('fetch', vi.fn(async () => ({ status: 404, body: null })));
			const view = mount(DOC, 0, CONTEXT, vi.fn());
			const at = DOC.indexOf(IMAGE);
			expect(await imageRequestAt(document.body, { from: 0, to: 6 })).toBeNull();
			view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new Event('error'));
			expect(await imageRequestAt(document.body, { from: at, to: at + IMAGE.length })).toBeNull();
		});
	});

	it('keeps the margin marker of a comment anchored on an image, and the comment still opens from it', async () => {
		const opened = vi.fn();
		const view = mount(DOC, 0);
		const at = DOC.indexOf(IMAGE);
		view.dispatch({ effects: setCommentRanges.of([{ from: at, to: at + IMAGE.length, id: 'c1', onMarkerFocus: opened }]) });
		await vi.waitFor(() => expect(view.dom.querySelector('.cm-comment-rail button')).not.toBeNull());
		view.dom.querySelector<HTMLButtonElement>('.cm-comment-rail button')!.click();
		expect(opened).toHaveBeenCalled();
		expect(opened.mock.calls[0][0]).toEqual(['c1']);
	});

	it('never draws or fetches a web or data URL, and shows it as a link', () => {
		const fetchSpy = vi.fn();
		vi.stubGlobal('fetch', fetchSpy);
		const doc = ['![remote](https://example.com/a.png)', '', '![inline](data:image/png;base64,AAAA)', '', '![proto](//cdn.example.com/a.png)'].join('\n');
		const view = mount(doc, doc.length);
		expect(view.dom.querySelector('img:not(.cm-widgetBuffer)')).toBeNull();
		expect(view.dom.querySelector('.cm-md-image')).toBeNull();
		const links = [...view.dom.querySelectorAll('.cm-md-link')].map((el) => el.textContent);
		expect(links).toEqual(expect.arrayContaining(['remote', 'inline', 'proto']));
		expect(fetchSpy).not.toHaveBeenCalled();
	});

	it('leaves an image as a link when the editor has no document to resolve against', () => {
		const view = mount(DOC, DOC.length, null);
		expect(view.dom.querySelector('img:not(.cm-widgetBuffer)')).toBeNull();
		expect([...view.dom.querySelectorAll('.cm-md-link')].map((el) => el.textContent)).toContain('Mock screen');
	});

	it('shows the alt text and "Image not found" for a file that is not there', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ status: 404, body: null })));
		const view = mount(DOC, 0);
		view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(view.dom.querySelector('.cm-md-image-note')?.textContent).toBe(`Mock screen · ${MISSING_IMAGE}`));
		expect(view.dom.querySelector('img:not(.cm-widgetBuffer)')).toBeNull();
	});

	it('says "Image not available here" when the server refuses the path as out of scope', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ status: 400, body: null })));
		const view = mount(`![Out](/etc/hosts.png)\n`, 0);
		view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(view.dom.querySelector('.cm-md-image-note')?.textContent).toBe(`Out · ${UNAVAILABLE_IMAGE}`));
	});

	it('says "Not an image" when the server answers 415 for a file that is not an image', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ status: 415, body: null })));
		const view = mount(`![Notes](notes.md)\n`, 0);
		view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(view.dom.querySelector('.cm-md-image-note')?.textContent).toBe(`Notes · ${NOT_AN_IMAGE}`));
	});

	it('says "Couldn\'t load this image" for a server error, not that the file is missing', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ status: 500, body: null })));
		const view = mount(`![Shot](shot.png)\n`, 0);
		view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(view.dom.querySelector('.cm-md-image-note')?.textContent).toBe(`Shot · ${FAILED_IMAGE}`));
	});

	it('says "Couldn\'t load this image" when the check itself cannot reach the server', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline'); }));
		const view = mount(`![](missing.png)\n`, 0);
		view.dom.querySelector('.cm-md-image img')!.dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(view.dom.querySelector('.cm-md-image-note')?.textContent).toBe(FAILED_IMAGE));
	});

	it('is decorations only: the text is the markdown, and an edit above an image keeps it drawn', () => {
		const view = mount(DOC, DOC.length);
		expect(view.state.doc.toString()).toBe(DOC);
		view.dispatch({ changes: { from: DOC.indexOf('# Plan') + 6, insert: ' typed by an agent' } });
		expect(view.state.doc.toString()).toBe(DOC.replace('# Plan', '# Plan typed by an agent'));
		expect(view.dom.querySelector('.cm-md-image img')).not.toBeNull();
		expect(view.contentDOM.textContent).not.toContain('![Mock screen]');
	});

	it('still marks a comment anchored on the text next to an image', () => {
		const view = mount(DOC, DOC.length);
		const from = DOC.indexOf('after the image');
		view.dispatch({ effects: setCommentRanges.of([{ from, to: from + 5, id: 'c1' }]) });
		expect(view.dom.querySelector('.cm-comment-anchor')?.textContent).toBe('after');
		// The anchor maps through an insert above the image, as it does without one.
		view.dispatch({ changes: { from: 0, insert: 'XX' } });
		expect(view.dom.querySelector('.cm-comment-anchor')?.textContent).toBe('after');
		expect(view.dom.querySelector('.cm-md-image img')).not.toBeNull();
	});

	it('draws two images on separate lines, and one image per source in a line', () => {
		const doc = '![one](a.png)\n\n![two](b.png) and ![three](c.png)\n';
		const view = mount(doc, doc.length);
		expect([...view.dom.querySelectorAll<HTMLImageElement>('.cm-md-image img')].map((img) => img.alt)).toEqual(['one', 'two', 'three']);
	});

	it('leaves an image inside a fenced block or a table as it was', () => {
		const doc = ['```', '![code](a.png)', '```', '', '| a |', '| - |', '| ![cell](b.png) |', ''].join('\n');
		const view = mount(doc, doc.length);
		expect(view.dom.querySelector('.cm-md-image')).toBeNull();
	});
});

describe('images in a rendered table', () => {
	const TABLE_DOC = ['# Shots', '', '| Phone | Desktop |', '| - | - |', '| ![A phone](img/a.png) | ![A desktop](/ws/other/b.png) |', ''].join('\n');
	const cells = (view: EditorView) => [...view.dom.querySelectorAll<HTMLImageElement>('.cm-md-table td img')];

	it('loads each cell image from the content route, a relative one from the document folder', () => {
		const imgs = cells(mount(TABLE_DOC, 0));
		expect(imgs.map((img) => img.alt)).toEqual(['A phone', 'A desktop']);
		expect(imgs.map(imagePath)).toEqual(['/ws/notes/img/a.png', '/ws/other/b.png']);
		expect(imgs.every((img) => img.src.includes('/api/files/content'))).toBe(true);
	});

	it('says why a cell image did not load', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ status: 404, body: null })));
		const view = mount(TABLE_DOC, 0);
		cells(view)[0].dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(view.dom.querySelector('.cm-md-table .cm-md-image-note')?.textContent).toBe(`A phone · ${MISSING_IMAGE}`));
	});

	it('never fetches a web image in a cell, or any image when the document has no path', () => {
		const web = ['| a |', '| - |', '| ![remote](https://example.com/x.png) |', ''].join('\n');
		expect(mount(web, 0).dom.querySelector('.cm-md-table img')).toBeNull();
		expect(mount(web, 0).dom.querySelector('.cm-md-table .cm-md-link')?.textContent).toBe('remote');
		expect(cells(mount(TABLE_DOC, 0, null))).toEqual([]);
	});
});
