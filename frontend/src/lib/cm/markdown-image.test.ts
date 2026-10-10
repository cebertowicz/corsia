import { describe, it, expect, afterEach, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { forceParsing } from '@codemirror/language';
import { GFM } from '@lezer/markdown';
import { commentMarks, setCommentRanges } from './comment-marks';
import { markdownLivePreview } from './markdown-live';
import { FAILED_IMAGE, imageContext, MISSING_IMAGE, NOT_AN_IMAGE, resolveImagePath, UNAVAILABLE_IMAGE, type ImageContext } from './markdown-image';

const DOC_PATH = '/ws/notes/plan.md';
const CONTEXT: ImageContext = { docPath: DOC_PATH, projectId: 'p1', token: 'tok' };
const views: EditorView[] = [];

function mount(doc: string, cursor?: number, context: ImageContext | null = CONTEXT): EditorView {
	const state = EditorState.create({
		doc,
		selection: cursor != null ? { anchor: cursor } : undefined,
		extensions: [markdown({ extensions: GFM }), markdownLivePreview, commentMarks, imageContext.of(() => context)],
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
	it('draws a relative image below its line, named by its alt text, from the content route', () => {
		const view = mount(DOC);
		const img = view.dom.querySelector<HTMLImageElement>('.cm-md-image img')!;
		expect(img.alt).toBe('Mock screen');
		expect(imagePath(img)).toBe('/ws/notes/shots/a.png');
		const params = new URL(img.src, 'http://x').searchParams;
		expect([params.get('raw'), params.get('project_id')]).toEqual(['true', 'p1']);
		expect(img.src).toContain('/api/files/content');
	});

	it('hides the source while the caret is off its line and shows it, still editable, on it', () => {
		const off = mount(DOC, 0);
		expect(off.contentDOM.textContent).not.toContain('![Mock screen]');
		expect(off.dom.querySelector('.cm-md-image img')).not.toBeNull();

		const on = mount(DOC, DOC.indexOf(IMAGE) + 3);
		expect(on.contentDOM.textContent).toContain(IMAGE);
		expect(on.dom.querySelector('.cm-md-image img')).not.toBeNull();
	});

	it('shows the source when the caret moves onto the line and hides it again when it leaves', () => {
		const view = mount(DOC, 0);
		view.dispatch({ selection: { anchor: DOC.indexOf(IMAGE) } });
		expect(view.contentDOM.textContent).toContain(IMAGE);
		view.dispatch({ selection: { anchor: DOC.length } });
		expect(view.contentDOM.textContent).not.toContain('![Mock screen]');
	});

	it('keeps the same image element across a caret move, so it does not reload', () => {
		const view = mount(DOC, 0);
		const before = view.dom.querySelector('.cm-md-image img');
		view.dispatch({ selection: { anchor: DOC.indexOf(IMAGE) } });
		expect(view.dom.querySelector('.cm-md-image img')).toBe(before);
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
