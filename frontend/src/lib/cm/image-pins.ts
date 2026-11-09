import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { commentRanges, type CommentRange } from './comment-marks';
import { cellImageRange, CELL_IMAGE, diagramAnchor } from './widget-open';
import { DIAGRAM } from './widget-blocks';
import { badgeFill } from '$lib/imageRegions';
import { viewerBadgeTags } from '$lib/imageGutter';

/** Where a pin's image is drawn: the markdown it comes from and its box, in scroller coordinates. */
type Drawn = { from: number; to: number; left: number; top: number; width: number; height: number };
type Placed = { range: CommentRange & { pin: NonNullable<CommentRange['pin']> }; x: number; y: number };

const overlaps = (a: { from: number; to: number }, b: { from: number; to: number }) => a.from < b.to && a.to > b.from;

/**
 * The drawn images a pin can sit on: an image in the text, an image in a table cell, a diagram. Each is measured by what it
 * shows (the image itself, the diagram's drawing), since a region is a fraction of that.
 */
function drawnImages(view: EditorView): Drawn[] {
	const scroller = view.scrollDOM.getBoundingClientRect();
	const at = (shown: Element | null, range: { from: number; to: number } | null): Drawn[] => {
		if (!shown || !range) return [];
		if (shown instanceof HTMLImageElement && !(shown.complete && shown.naturalWidth > 0)) return [];
		const box = shown.getBoundingClientRect();
		if (!box.width || !box.height) return [];
		return [{ ...range, left: box.left - scroller.left + view.scrollDOM.scrollLeft, top: box.top - scroller.top + view.scrollDOM.scrollTop, width: box.width, height: box.height }];
	};
	const content = view.contentDOM;
	return [
		...[...content.querySelectorAll<HTMLElement>('.cm-md-image')].flatMap((wrap) => at(wrap.querySelector('img'), { from: Number(wrap.dataset.from), to: Number(wrap.dataset.to) })),
		...[...content.querySelectorAll<HTMLElement>(CELL_IMAGE)].flatMap((wrap) => at(wrap.querySelector('img'), cellImageRange(view, wrap))),
		...[...content.querySelectorAll<HTMLElement>(DIAGRAM)].flatMap((el) => at(el.querySelector('svg'), diagramAnchor(view, el))),
	];
}

/**
 * The numbered pins of image and diagram comments, drawn on the image in the document where it is: an image in the text, in a
 * table cell, or a diagram, at any width. Each is the image viewer's disc, in its author's colour; a press opens that thread as a
 * press on a highlight does. The layer sits in the scroller, so the pins move with the text.
 */
export function imagePins(hooks: { onFocus: (ids: string[], opener: HTMLElement, click?: MouseEvent) => void }) {
	return ViewPlugin.fromClass(class {
		private readonly dom: HTMLElement;
		private pins: HTMLButtonElement[] = [];
		private readonly resize?: ResizeObserver;
		private readonly loaded = () => this.schedule();

		constructor(private readonly view: EditorView) {
			this.dom = document.createElement('div');
			this.dom.className = 'cm-image-pins';
			view.scrollDOM.append(this.dom);
			// An image that loads, or a text size change, moves what a pin is drawn on without an editor update.
			view.dom.addEventListener('load', this.loaded, true);
			if (typeof ResizeObserver !== 'undefined') {
				this.resize = new ResizeObserver(this.loaded);
				this.resize.observe(view.contentDOM);
			}
			this.schedule();
		}

		update(update: ViewUpdate) {
			const ranges = update.startState.field(commentRanges) !== update.state.field(commentRanges);
			if (ranges || update.docChanged || update.viewportChanged || update.heightChanged || update.geometryChanged) this.schedule();
		}

		destroy() {
			this.view.dom.removeEventListener('load', this.loaded, true);
			this.resize?.disconnect();
			this.dom.remove();
		}

		private schedule() {
			this.view.requestMeasure<Placed[]>({ key: this, read: (view) => this.read(view), write: (placed) => this.write(placed) });
		}

		private read(view: EditorView): Placed[] {
			const pinned = view.state.field(commentRanges).filter((range): range is Placed['range'] => !!range.pin);
			if (!pinned.length) return [];
			return drawnImages(view).flatMap((image) => {
				const mine = pinned.filter((range) => overlaps(range, image)).sort((a, b) => a.pin.number - b.pin.number);
				const tags = viewerBadgeTags(mine.map((range) => range.pin.region), { width: image.width, height: image.height });
				return mine.map((range, index) => ({ range, x: image.left + tags[index].x, y: image.top + tags[index].y }));
			});
		}

		private write(placed: Placed[]) {
			while (this.pins.length > placed.length) this.pins.pop()!.remove();
			while (this.pins.length < placed.length) {
				const pin = document.createElement('button');
				pin.type = 'button';
				pin.className = 'cm-image-pin';
				pin.append(Object.assign(document.createElement('span'), { className: 'disc' }));
				pin.addEventListener('mousedown', (event) => event.preventDefault());
				pin.addEventListener('click', (event) => {
					event.stopPropagation();
					const id = pin.dataset.commentId;
					if (id) hooks.onFocus([id], pin, event);
				});
				this.dom.append(pin);
				this.pins.push(pin);
			}
			placed.forEach(({ range, x, y }, index) => {
				const pin = this.pins[index];
				pin.dataset.commentId = range.id;
				pin.classList.toggle('selected', !!range.focused);
				pin.setAttribute('aria-pressed', String(!!range.focused));
				pin.setAttribute('aria-label', `Comment ${range.pin.number}`);
				pin.style.left = `${x}px`;
				pin.style.top = `${y}px`;
				pin.style.setProperty('--region-color', range.pin.color);
				pin.style.setProperty('--disc-color', badgeFill(range.pin.color));
				pin.firstElementChild!.textContent = String(range.pin.number);
			});
		}
	});
}
