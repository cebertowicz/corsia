import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState, StateField, type Extension } from '@codemirror/state';
import { Decoration, EditorView, WidgetType } from '@codemirror/view';
import { commentMarks, commentRanges, setCommentRanges, type CommentRange } from './comment-marks';
import { commentRail } from './comment-rail';

const views: EditorView[] = [];

function mount(doc: string, extra: Extension = []) {
	const view = new EditorView({ state: EditorState.create({ doc, extensions: [commentMarks, commentRail, extra] }), parent: document.body });
	views.push(view);
	return view;
}

const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
const lines = (view: EditorView) => [...view.dom.querySelectorAll<HTMLElement>('.cm-comment-line')];

async function show(view: EditorView, ranges: CommentRange[]) {
	view.dispatch({ effects: setCommentRanges.of(ranges) });
	await frame();
}

afterEach(() => {
	views.splice(0).forEach((view) => view.destroy());
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

/** A line drawn as a block widget, as an image alone on its line is. */
class Block extends WidgetType {
	toDOM() {
		const wrap = document.createElement('div');
		wrap.className = 'block-widget';
		return wrap;
	}
}

const blockLine = (from: number, to: number) => StateField.define({
	create: () => Decoration.set([Decoration.replace({ widget: new Block(), block: true }).range(from, to)]),
	update: (decorations) => decorations,
	provide: (field) => EditorView.decorations.from(field),
});

/** jsdom has no ResizeObserver; this one records what is watched and reports a resize on demand. */
function stubResizeObserver() {
	const observers: { callback: () => void; watched: Set<Element> }[] = [];
	vi.stubGlobal('ResizeObserver', class {
		private readonly entry: { callback: () => void; watched: Set<Element> };
		constructor(callback: () => void) { this.entry = { callback, watched: new Set() }; observers.push(this.entry); }
		observe(element: Element) { this.entry.watched.add(element); }
		unobserve(element: Element) { this.entry.watched.delete(element); }
		disconnect() { this.entry.watched.clear(); }
	});
	return {
		watches: (element: Element) => observers.some((observer) => observer.watched.has(element)),
		resize: (element: Element) => observers.filter((observer) => observer.watched.has(element)).forEach((observer) => observer.callback()),
	};
}

const placeAt = (element: Element, top: number) => {
	element.getBoundingClientRect = () => ({ top, bottom: top + 200, left: 0, right: 600, height: 200, width: 600, x: 0, y: top, toJSON: () => ({}) });
};

describe('comment rail', () => {
	it('draws nothing a person can press or focus: one empty hidden element per commented line', async () => {
		const view = mount(`alpha${"\n".repeat(60)}beta`);
		await show(view, [{ from: 0, to: 5, id: 'a', onMarkerFocus: vi.fn() }]);
		expect(view.dom.querySelector('.cm-comment-rail')?.querySelector('button, svg, [tabindex]')).toBeNull();
		expect(view.dom.querySelector('.cm-comment-rail')?.getAttribute('aria-hidden')).toBe('true');
		const [only] = lines(view);
		expect(only.dataset.commentIds).toBe('a');
		expect(only.textContent).toBe('');
	});

	it('lists the threads of one line together and keeps each thread once', async () => {
		const view = mount('alpha');
		await show(view, [{ from: 0, to: 2, id: 'a' }, { from: 2, to: 5, id: 'b' }, { from: 1, to: 4, id: 'a' }]);
		expect(lines(view)).toHaveLength(1);
		expect(lines(view)[0].dataset.commentIds).toBe('a,b');
	});

	it('flags a line whose threads have all lost their text', async () => {
		const view = mount('alpha\nbeta');
		await show(view, [{ from: 0, to: 5, id: 'a' }, { from: 6, to: 6, id: 'b', point: true, lost: true }]);
		expect(lines(view).map((line) => [line.dataset.commentIds, line.dataset.orphan ?? null])).toEqual([['a', null], ['b', 'true']]);
		await show(view, [{ from: 0, to: 5, id: 'a' }, { from: 1, to: 1, id: 'b', point: true, lost: true }]);
		expect(lines(view).map((line) => [line.dataset.commentIds, line.dataset.orphan ?? null])).toEqual([['a,b', null]]);
	});

	it('moves a point range with the text around it and keeps it when that text is deleted', async () => {
		const view = mount('alpha\nbeta\ngamma');
		await show(view, [{ from: 6, to: 6, id: 'gone', point: true, lost: true }]);
		view.dispatch({ changes: { from: 0, to: 0, insert: 'start ' } });
		expect(view.state.field(commentRanges)).toMatchObject([{ id: 'gone', from: 12, to: 12 }]);
		view.dispatch({ changes: { from: 6, to: 17 } });
		expect(view.state.field(commentRanges)).toMatchObject([{ id: 'gone', from: 6, to: 6 }]);
	});

	it('drops the line together with the highlight when an edit collapses its anchor', async () => {
		const view = mount('alpha\nbeta');
		await show(view, [{ from: 0, to: 5, id: 'a' }, { from: 6, to: 10, id: 'b' }]);
		expect(lines(view).map((line) => line.dataset.commentIds)).toEqual(['a', 'b']);
		view.dispatch({ changes: { from: 0, to: 5 } });
		await frame();
		expect(view.dom.querySelectorAll('.cm-comment-anchor')).toHaveLength(1);
		expect(lines(view).map((line) => line.dataset.commentIds)).toEqual(['b']);
	});

	it('puts a line drawn as a block widget at the widget, and moves it when the widget resizes', async () => {
		const observer = stubResizeObserver();
		const view = mount('alpha\nimage\ngamma', blockLine(6, 11));
		const widget = view.contentDOM.querySelector('.block-widget')!;
		placeAt(widget, 300);
		await show(view, [{ from: 6, to: 11, id: 'a' }]);
		expect(lines(view)[0].style.top).toBe('300px');
		// An image above that loads, or a theme that changes the widget's size, moves it without any editor update.
		expect(observer.watches(widget)).toBe(true);
		placeAt(widget, 344);
		observer.resize(widget);
		await frame();
		expect(lines(view)[0].style.top).toBe('344px');
	});

	it('regroups when an uncommented block widget above a commented line resizes', async () => {
		const observer = stubResizeObserver();
		const view = mount('image\nalpha', blockLine(0, 5));
		const widget = view.contentDOM.querySelector('.block-widget')!;
		const line = view.contentDOM.querySelector('.cm-line')!;
		placeAt(line, 20);
		await show(view, [{ from: 6, to: 11, id: 'a' }]);
		expect(lines(view)[0].style.top).toBe('20px');
		// The image above loads: the commented line moves down while the text's height may stay pinned at its minimum.
		expect(observer.watches(widget)).toBe(true);
		placeAt(line, 220);
		observer.resize(widget);
		await frame();
		expect(lines(view)[0].style.top).toBe('220px');
	});
});
