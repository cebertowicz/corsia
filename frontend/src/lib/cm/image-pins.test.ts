import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState, StateField } from '@codemirror/state';
import { Decoration, EditorView, WidgetType } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { commentMarks, setCommentRanges, type CommentRange } from './comment-marks';
import { imagePins } from './image-pins';
import { viewerBadgeTags } from '$lib/imageGutter';
import { badgeFill } from '$lib/imageRegions';

const views: EditorView[] = [];
const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
const box = (left: number, top: number, width: number, height: number) => () => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) });

/** An image in the text as `markdown-image.ts` draws it: its markdown range on the wrap, the image inside, drawn or not. */
class Image extends WidgetType {
	constructor(readonly from: number, readonly to: number, readonly drawn: boolean) { super(); }
	toDOM() {
		const wrap = document.createElement('div');
		wrap.className = 'cm-md-image';
		Object.assign(wrap.dataset, { from: String(this.from), to: String(this.to) });
		const img = document.createElement('img');
		Object.defineProperty(img, 'complete', { value: true });
		Object.defineProperty(img, 'naturalWidth', { value: this.drawn ? 400 : 0 });
		img.getBoundingClientRect = box(40, 100, 400, 200);
		wrap.append(img);
		return wrap;
	}
}

/** A diagram as the mermaid widget draws it: its drawing is an `svg`. */
class Diagram extends WidgetType {
	toDOM() {
		const el = document.createElement('div');
		el.className = 'cm-md-mermaid';
		const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		svg.getBoundingClientRect = box(60, 300, 300, 150);
		el.append(svg);
		return el;
	}
}

const replaced = (widget: WidgetType, from: number, to: number) => StateField.define({
	create: () => Decoration.set([Decoration.replace({ widget, block: true }).range(from, to)]),
	update: (decorations) => decorations,
	provide: (field) => EditorView.decorations.from(field),
});

function mount(doc: string, widget: StateField<unknown>, onFocus = vi.fn()) {
	const view = new EditorView({ state: EditorState.create({ doc, extensions: [markdown(), commentMarks, widget, imagePins({ onFocus })] }), parent: document.body });
	view.scrollDOM.getBoundingClientRect = box(0, 0, 800, 600);
	views.push(view);
	return { view, onFocus };
}

async function show(view: EditorView, ranges: CommentRange[]) {
	view.dispatch({ effects: setCommentRanges.of(ranges) });
	await frame();
}

const pins = (view: EditorView) => [...view.scrollDOM.querySelectorAll<HTMLButtonElement>('.cm-image-pins .cm-image-pin')];
const IMAGE = '![chart](chart.png)';
const point = { x: 0.5, y: 0.25, w: 0, h: 0 };

afterEach(() => {
	vi.restoreAllMocks();
	Reflect.deleteProperty(Range.prototype, 'getClientRects');
	Reflect.deleteProperty(Range.prototype, 'getBoundingClientRect');
	views.splice(0).forEach((view) => view.destroy());
	document.body.innerHTML = '';
});

describe('image pins', () => {
	it('draws each pin on the image in the text where the image viewer places it, numbered and in its author colour', async () => {
		const { view } = mount(`${IMAGE}\n`, replaced(new Image(0, IMAGE.length, true), 0, IMAGE.length));
		await show(view, [{ from: 0, to: IMAGE.length, id: 'c1', region: true, pin: { region: point, number: 1, color: '#3366ff' } }]);
		const [pin] = pins(view);
		expect(pin.dataset.commentId).toBe('c1');
		expect(pin.textContent).toBe('1');
		expect(pin.getAttribute('aria-label')).toBe('Comment 1');
		expect(pin.style.getPropertyValue('--region-color')).toBe('#3366ff');
		expect(pin.style.getPropertyValue('--disc-color')).toBe(badgeFill('#3366ff'));
		const [tag] = viewerBadgeTags([point], { width: 400, height: 200 });
		expect([pin.style.left, pin.style.top]).toEqual([`${40 + tag.x}px`, `${100 + tag.y}px`]);
	});

	it('draws nothing on an image that has not drawn, and nothing for a comment with no pin', async () => {
		const { view } = mount(`${IMAGE}\n`, replaced(new Image(0, IMAGE.length, false), 0, IMAGE.length));
		await show(view, [{ from: 0, to: IMAGE.length, id: 'c1', region: true, pin: { region: point, number: 1, color: '#3366ff' } }]);
		expect(pins(view)).toHaveLength(0);
		const drawn = mount(`${IMAGE}\n`, replaced(new Image(0, IMAGE.length, true), 0, IMAGE.length)).view;
		await show(drawn, [{ from: 0, to: IMAGE.length, id: 'whole' }]);
		expect(pins(drawn)).toHaveLength(0);
	});

	it('opens its thread on a press, as a press on a highlight does, and rings the picked one', async () => {
		const { view, onFocus } = mount(`${IMAGE}\n`, replaced(new Image(0, IMAGE.length, true), 0, IMAGE.length));
		const pinned = (id: string, number: number, focused = false): CommentRange => ({ from: 0, to: IMAGE.length, id, region: true, focused, pin: { region: { ...point, x: number / 4 }, number, color: '#3366ff' } });
		await show(view, [pinned('c2', 2, true), pinned('c1', 1)]);
		expect(pins(view).map((pin) => [pin.dataset.commentId, pin.classList.contains('selected'), pin.getAttribute('aria-pressed')])).toEqual([['c1', false, 'false'], ['c2', true, 'true']]);
		pins(view)[0].click();
		expect(onFocus).toHaveBeenCalledWith(['c1'], pins(view)[0], expect.any(MouseEvent));
	});

	it('draws a diagram pin on the diagram its fence holds', async () => {
		const doc = '```mermaid\ngraph TD; A-->B\n```\n';
		const fence = doc.indexOf('\n', doc.indexOf('B'));
		const { view } = mount(doc, replaced(new Diagram(), 0, fence + 4));
		const head = doc.indexOf('\n');
		await show(view, [{ from: 0, to: head, id: 'd1', region: true, pin: { region: point, number: 3, color: '#aa3300' } }]);
		const [pin] = pins(view);
		expect(pin?.textContent).toBe('3');
		const [tag] = viewerBadgeTags([point], { width: 300, height: 150 });
		expect([pin.style.left, pin.style.top]).toEqual([`${60 + tag.x}px`, `${300 + tag.y}px`]);
	});

	it('draws the same sign, unnumbered, past the end of the line a deleted passage\'s thread sits at, and opens it on a press', async () => {
		const doc = 'First line.\nThe second line.\n';
		// The editor measures its text lines with ranges, which this DOM does not lay out.
		const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
		layout.getClientRects = () => [] as unknown as DOMRectList;
		layout.getBoundingClientRect = () => box(0, 0, 0, 0)() as DOMRect;
		const { view, onFocus } = mount(doc, StateField.define({ create: () => Decoration.none, update: (value) => value }));
		view.contentDOM.getBoundingClientRect = box(20, 0, 700, 600);
		const lineEnd = doc.indexOf('\n', 12);
		vi.spyOn(view, 'coordsAtPos').mockImplementation((pos) => pos === lineEnd ? { left: 180, right: 180, top: 40, bottom: 60 } : null);
		await show(view, [{ from: 16, to: 16, id: 'gone', point: true }, { from: 0, to: 5, id: 'kept' }]);
		const [sign] = pins(view);
		expect(pins(view)).toHaveLength(1);
		expect(sign.dataset.commentId).toBe('gone');
		expect(sign.classList.contains('removed')).toBe(true);
		expect(sign.textContent).toBe('');
		expect(sign.getAttribute('aria-label')).toBe('Comment on text that isn’t in the document');
		// Centred on the line, a disc's width past its last character, so no character is covered and no line moves.
		expect([sign.style.left, sign.style.top]).toEqual(['195px', '50px']);
		sign.click();
		expect(onFocus).toHaveBeenCalledWith(['gone'], sign, expect.any(MouseEvent));
	});
});
