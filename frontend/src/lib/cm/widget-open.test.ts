import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { forceParsing } from '@codemirror/language';
import { GFM } from '@lezer/markdown';
import { markdownLivePreview } from './markdown-live';
import { imageContext, imageOpener, type ImageContext, type OpenImageRequest } from './markdown-image';
import { cellImageRange, markOpenable, openWidgetImage, openWidgetOnKey } from './widget-open';
import { widgetComments } from './widget-comments';
import { commentRanges } from './comment-marks';
import { pendingCommentMark } from './pending-comment-mark';

// Keep diagram rendering pending until the test inserts its SVG.
vi.mock('$lib/mermaid', () => ({ mountMermaid: () => ({ destroy: () => {} }) }));

const CONTEXT: ImageContext = { docPath: '/ws/notes/plan.md', projectId: 'p1', token: 'test-token' };
const IMAGE = '![Home screen](shots/a.png)';
const DOC = `# Plan\n\n| Screen | Notes |\n| --- | --- |\n| ${IMAGE} | main path |\n| Settings | ${IMAGE} |\n\nafter\n`;
const views: EditorView[] = [];

function mount(opener: (request: OpenImageRequest) => void, doc = DOC, comments = false): EditorView {
	const view = new EditorView({
		state: EditorState.create({ doc, extensions: [markdown({ extensions: GFM }), markdownLivePreview, imageContext.of(() => CONTEXT), imageOpener.of(opener), ...(comments ? [commentRanges, pendingCommentMark, widgetComments({ onSelect: () => {}, onFocus: () => {} })] : [])] }),
		parent: document.body,
	});
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	views.push(view);
	return view;
}
/** jsdom loads nothing: a drawn image is one that reports itself complete and sized. */
function drawn(wrap: HTMLElement) {
	const img = wrap.querySelector('img')!;
	Object.defineProperty(img, 'complete', { value: true });
	Object.defineProperty(img, 'naturalWidth', { value: 1600 });
}
const nth = (n: number) => { let at = -1; for (let k = 0; k <= n; k++) at = DOC.indexOf(IMAGE, at + 1); return at; };

afterEach(() => { while (views.length) views.pop()!.destroy(); });

describe('an image inside a table cell', () => {
	it('is drawn as a cell image carrying its source and alt text', () => {
		const view = mount(() => undefined);
		const wraps = view.dom.querySelectorAll<HTMLElement>('.cm-md-cell-image');
		expect(wraps).toHaveLength(2);
		expect(wraps[0].dataset.alt).toBe('Home screen');
		expect(wraps[0].dataset.src).toContain('a.png');
	});

	it('maps each cell image to its own markdown in the table', () => {
		const view = mount(() => undefined);
		const [first, second] = [...view.dom.querySelectorAll<HTMLElement>('.cm-md-cell-image')];
		expect(cellImageRange(view, first)).toEqual({ from: nth(0), to: nth(0) + IMAGE.length });
		expect(cellImageRange(view, second)).toEqual({ from: nth(1), to: nth(1) + IMAGE.length });
	});

	it('does not open HTML images or shift the inline image after them', () => {
		const html = '<img src="shots/a.png" alt="HTML">';
		const inline = '![Inline](shots/a.png)';
		const doc = `# Test\n\n| Image |\n| --- |\n| ${html} |\n| ${html} ${inline} |\n| ${inline} |\n`;
		const requests: OpenImageRequest[] = [];
		const view = mount((request) => requests.push(request), doc);
		const wraps = [...view.dom.querySelectorAll<HTMLElement>('.cm-md-cell-image')];
		expect(wraps).toHaveLength(4);
		wraps.forEach(drawn);
		for (const wrap of wraps) markOpenable(wrap, view);
		expect(wraps[0].hasAttribute('tabindex')).toBe(false);
		expect(wraps[1].hasAttribute('tabindex')).toBe(false);
		expect(openWidgetImage(view, wraps[0])).toBe(false);
		expect(openWidgetImage(view, wraps[1])).toBe(false);
		expect(openWidgetImage(view, wraps[2])).toBe(true);
		expect(requests[0].from).toBe(doc.indexOf(inline));
		expect(openWidgetImage(view, wraps[3])).toBe(true);
		expect(requests[1].from).toBe(doc.lastIndexOf(inline));
	});

	it('opens the viewer on its markdown when it has drawn, and not before', () => {
		const requests: OpenImageRequest[] = [];
		const view = mount((request) => requests.push(request));
		const wrap = view.dom.querySelector<HTMLElement>('.cm-md-cell-image')!;
		expect(openWidgetImage(view, wrap.querySelector('img'))).toBe(false);
		drawn(wrap);
		expect(openWidgetImage(view, wrap.querySelector('img'))).toBe(true);
		expect(requests).toHaveLength(1);
		expect(requests[0]).toMatchObject({ alt: 'Home screen', from: nth(0), to: nth(0) + IMAGE.length });
	});
});

describe('opening a delayed diagram from the keyboard', () => {
	it('does not rewrite openability when another render leaves the diagram unchanged', async () => {
		const view = mount(() => undefined, '# Plan\n\n```mermaid\ngraph TD; A-->B\n```\n', true);
		const diagram = view.dom.querySelector<HTMLElement>('.cm-md-mermaid')!;
		diagram.append(document.createElementNS('http://www.w3.org/2000/svg', 'svg'));
		await vi.waitFor(() => expect(diagram.dataset.openable).toBe('true'));
		const set = vi.spyOn(diagram, 'setAttribute');
		view.dom.dispatchEvent(new Event('load'));
		await new Promise<void>((resolve) => view.requestMeasure({ read: () => undefined, write: () => resolve() }));
		expect(set).not.toHaveBeenCalled();
	});
	it('becomes a tab stop when its SVG is inserted after the initial paint', async () => {
		const doc = '# Plan\n\n```mermaid\nflowchart LR\n A --> B\n```\n';
		const view = mount(() => undefined, doc, true);
		const diagram = view.dom.querySelector<HTMLElement>('.cm-md-mermaid')!;
		expect(diagram).toBeTruthy();
		expect(diagram.dataset.diagramSource).toBe('flowchart LR\n A --> B');
		await new Promise<void>((resolve) => view.requestMeasure({ read: () => undefined, write: () => resolve() }));
		expect(diagram.hasAttribute('tabindex')).toBe(false);
		diagram.append(document.createElementNS('http://www.w3.org/2000/svg', 'svg'));
		await vi.waitFor(() => expect(diagram.getAttribute('tabindex')).toBe('0'));
		expect(diagram.getAttribute('role')).toBe('button');
		expect(diagram.getAttribute('aria-label')).toBe('Open diagram larger');
	});
});

describe('opening a cell image from the keyboard', () => {
	it('makes a delayed image reachable after its load event', async () => {
		const view = mount(() => undefined, DOC, true);
		const wrap = view.dom.querySelector<HTMLElement>('.cm-md-cell-image')!;
		expect(wrap.hasAttribute('tabindex')).toBe(false);
		drawn(wrap);
		wrap.querySelector('img')!.dispatchEvent(new Event('load'));
		await vi.waitFor(() => expect(wrap.getAttribute('tabindex')).toBe('0'));
	});

	const key = (view: EditorView, target: HTMLElement, name: string) => {
		const event = new KeyboardEvent('keydown', { key: name });
		Object.defineProperty(event, 'target', { value: target });
		return openWidgetOnKey(view, event);
	};

	it('makes a drawn cell image a labelled tab stop, and one that has not drawn none', () => {
		const view = mount(() => undefined);
		const wrap = view.dom.querySelector<HTMLElement>('.cm-md-cell-image')!;
		markOpenable(wrap, view);
		expect(wrap.hasAttribute('tabindex')).toBe(false);
		drawn(wrap);
		markOpenable(wrap, view);
		expect(wrap.getAttribute('tabindex')).toBe('0');
		expect(wrap.getAttribute('role')).toBe('button');
		expect(wrap.getAttribute('aria-label')).toBe('Open image larger: Home screen');
		wrap.replaceChildren(document.createElement('div')); // a failed load swaps the image for its note
		markOpenable(wrap, view);
		expect(wrap.hasAttribute('tabindex')).toBe(false);
	});

	it('opens on Enter and Space when the cell image itself has focus, and on no other key', () => {
		const requests: OpenImageRequest[] = [];
		const view = mount((request) => requests.push(request));
		const wrap = view.dom.querySelector<HTMLElement>('.cm-md-cell-image')!;
		drawn(wrap);
		markOpenable(wrap, view);
		expect(key(view, wrap, 'a')).toBe(false);
		expect(key(view, wrap, 'Enter')).toBe(true);
		expect(key(view, wrap, ' ')).toBe(true);
		expect(requests).toHaveLength(2);
		expect(requests[0]).toMatchObject({ alt: 'Home screen', from: nth(0) });
	});
});
