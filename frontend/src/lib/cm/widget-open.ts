import { EditorView } from '@codemirror/view';
import { imageContext, imageOpener, type OpenImageRequest } from './markdown-image';
import { blockRange, DIAGRAM, TABLE } from './widget-blocks';
import { tableCells } from './widget-cells';
import { diagramElements } from '$lib/diagramSubject';
import { cellImageSlots } from './widget-cell-images';

/**
 * An image inside a table cell and a rendered diagram open the image viewer as an image in the text does (TDC-FR6, FR7).
 * Both sit in widgets that take no editor events, so this reads them back to the source: the viewer, its pins and the
 * comments on them all find the image by its range in the document.
 */

export const CELL_IMAGE = '.cm-md-cell-image';

/**
 * Makes a drawn cell image or diagram a tab stop that Enter or Space opens, as a click does, and one that has not drawn
 * (or failed) not one. Painted after the editor lays the widgets out, since a diagram's comment paint resets its role.
 */
export function markOpenable(el: HTMLElement, view: EditorView) {
	const cell = el.matches(CELL_IMAGE), drawn = cell ? drawnCellImage(el) && cellImageRange(view, el) : drawnDiagram(el);
	if (!drawn) {
		if (el.dataset.openable) { for (const name of ['tabindex', 'role', 'aria-label', 'data-openable']) el.removeAttribute(name); }
		return;
	}
	const label = cell ? `Open image larger${el.dataset.alt ? `: ${el.dataset.alt}` : ''}` : el.dataset.commentAria ? 'Commented diagram. Open larger' : 'Open diagram larger';
	el.setAttribute('tabindex', '0');
	el.setAttribute('role', 'button');
	el.setAttribute('aria-label', label);
	el.dataset.openable = 'true';
}

/** Enter or Space on a focused cell image or diagram opens it; false for any other key or target. */
export function openWidgetOnKey(view: EditorView, event: KeyboardEvent): boolean {
	const el = event.target instanceof HTMLElement && event.target.dataset.openable ? event.target : null;
	if (!el || event.altKey || event.ctrlKey || event.metaKey || (event.key !== 'Enter' && event.key !== ' ')) return false;
	return openWidgetImage(view, el);
}

/** A cell image that has drawn; one still loading or failed has nothing to enlarge. */
export function drawnCellImage(target: EventTarget | null): HTMLElement | null {
	const wrap = (target as Element | null)?.closest?.<HTMLElement>(CELL_IMAGE) ?? null;
	const img = wrap?.querySelector('img');
	return wrap && img && img.complete && img.naturalWidth > 0 ? wrap : null;
}

/** A diagram that has drawn. */
export const drawnDiagram = (target: EventTarget | null): HTMLElement | null => {
	const el = (target as Element | null)?.closest?.<HTMLElement>(DIAGRAM) ?? null;
	return el && el.querySelector('svg') && !el.classList.contains('is-error') ? el : null;
};

function geometry(view: EditorView, el: HTMLElement) {
	return { scrollTop: view.scrollDOM.scrollTop, top: el.getBoundingClientRect().top - view.scrollDOM.getBoundingClientRect().top };
}

/** The markdown range of a cell image: the table's source, the cell it is in, and which of that cell's images it is. */
export function cellImageRange(view: EditorView, wrap: HTMLElement): { from: number; to: number } | null {
	const table = wrap.closest<HTMLElement>(TABLE);
	const cell = wrap.closest<HTMLTableCellElement>('td, th');
	const row = cell?.parentElement as HTMLTableRowElement | null;
	const block = table && blockRange(view, table);
	const context = view.state.facet(imageContext)();
	if (!block || !cell || !row || !context) return null;
	const src = view.state.sliceDoc(block.from, block.to);
	const span = tableCells(src).find((item) => item.row === row.rowIndex && item.col === cell.cellIndex);
	if (!span) return null;
	const slots = cellImageSlots(src.slice(span.from, span.to), context.docPath);
	const at = slots[[...cell.querySelectorAll(CELL_IMAGE)].indexOf(wrap)];
	return at ? { from: block.from + span.from + at.from, to: block.from + span.from + at.to } : null;
}

/** What opening a cell image needs: the same request an image in the text gives. */
export function cellImageRequest(view: EditorView, wrap: HTMLElement): OpenImageRequest | null {
	const range = cellImageRange(view, wrap);
	return range ? { src: wrap.dataset.src ?? '', alt: wrap.dataset.alt ?? '', ...range, ...geometry(view, wrap) } : null;
}

/** The first line of a diagram's fence: where a pin on it is anchored, and what stays put when its source is edited. */
export function diagramAnchor(view: EditorView, el: HTMLElement): { from: number; to: number } | null {
	const block = blockRange(view, el);
	if (!block) return null;
	const line = view.state.doc.lineAt(block.from);
	return { from: line.from, to: line.to };
}

/** Puts keyboard focus back on the cell image or diagram at `range`, where Back leaves the reader; false when none is there. */
export function focusWidgetAt(root: HTMLElement, { from, to }: { from: number; to: number }): boolean {
	const editor = root.querySelector<HTMLElement>('.cm-editor');
	const view = editor ? EditorView.findFromDOM(editor) : null;
	if (!view || from > view.state.doc.length) return false;
	const overlaps = (range: { from: number; to: number } | null) => !!range && range.from < to && range.to > from;
	const found = [...view.contentDOM.querySelectorAll<HTMLElement>(CELL_IMAGE)].find((wrap) => overlaps(cellImageRange(view, wrap)))
		?? [...view.contentDOM.querySelectorAll<HTMLElement>(DIAGRAM)].find((el) => overlaps(diagramAnchor(view, el)));
	if (!found?.dataset.openable) return false;
	found.focus({ preventScroll: true });
	return true;
}

const SNAPSHOT_WIDTH = 1200;

/**
 * The diagram as it is drawn now, as an image the viewer can show: its SVG at a size the viewer fits and zooms. It is a
 * snapshot, so it keeps the theme it was drawn in and does not follow the source after it is taken.
 */
export function diagramSnapshot(el: HTMLElement): string | null {
	const svg = el.querySelector('svg');
	return svg ? snapshotOf(svg) : null;
}

/** The same snapshot from the SVG a diagram renders to, for a diagram not drawn in the document now (the drawer's thumbnail). */
export function diagramMarkupSnapshot(markup: string): string | null {
	const host = document.createElement('div');
	host.innerHTML = markup;
	const svg = host.querySelector('svg');
	return svg ? snapshotOf(svg) : null;
}

/** The diagram's own size, and how much larger than it the snapshot is drawn so the viewer has pixels to zoom into. */
function snapshotSize(svg: SVGSVGElement): { width: number; height: number; scale: number } | null {
	const box = svg.viewBox.baseVal, rect = svg.getBoundingClientRect();
	const [width, height] = [box?.width || rect.width, box?.height || rect.height];
	if (!(width > 0 && height > 0)) return null;
	return { width, height, scale: Math.min(4, Math.max(1, SNAPSHOT_WIDTH / width)) };
}

function snapshotOf(svg: SVGSVGElement): string | null {
	const size = snapshotSize(svg);
	if (!size) return null;
	const copy = svg.cloneNode(true) as SVGSVGElement;
	copy.removeAttribute('style');
	copy.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
	copy.setAttribute('width', String(Math.round(size.width * size.scale)));
	copy.setAttribute('height', String(Math.round(size.height * size.scale)));
	return URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)], { type: 'image/svg+xml' }));
}

/** The viewer's scale at which a diagram's labels are the size they are drawn at in the document: its own size, never above it. */
export function readableScaleOf(svg: SVGSVGElement): number | undefined {
	const size = snapshotSize(svg);
	return size ? 1 / size.scale : undefined;
}

/** What opening a diagram needs: its snapshot, and its opening fence line as the range its comments are anchored on. */
export function diagramRequest(view: EditorView, el: HTMLElement): OpenImageRequest | null {
	const range = diagramAnchor(view, el), svg = el.querySelector('svg');
	if (!range || !svg) return null;
	const src = snapshotOf(svg);
	return src ? { src, alt: 'Diagram', ...range, ...geometry(view, el), readableScale: readableScaleOf(svg), diagramElements: diagramElements(svg) } : null;
}

/** Opens the cell image or diagram a click landed on; false when it is not one that can open. */
export function openWidgetImage(view: EditorView, target: EventTarget | null): boolean {
	const open = view.state.facet(imageOpener);
	if (!open) return false;
	const cellImage = drawnCellImage(target), diagram = cellImage ? null : drawnDiagram(target);
	const request = cellImage ? cellImageRequest(view, cellImage) : diagram ? diagramRequest(view, diagram) : null;
	if (!request) return false;
	open(request);
	return true;
}

/** How long a cell image scrolled into view may take to draw before opening it is given up. */
const DRAW_WAIT_MS = 4000;

/**
 * What opening the cell image or diagram at `from`..`to` needs, as a click on it would give: for a comment on it, opened from the
 * margin or the comments drawer. One off screen is scrolled to first and waited for; null when none draws there.
 */
export async function widgetImageRequestAt(root: HTMLElement, { from, to }: { from: number; to: number }): Promise<OpenImageRequest | null> {
	const editor = root.querySelector<HTMLElement>('.cm-editor');
	const view = editor ? EditorView.findFromDOM(editor) : null;
	if (!view || from > view.state.doc.length) return null;
	// 'waiting' while the image or diagram there has not drawn yet; null when nothing is there.
	const found = (): OpenImageRequest | 'waiting' | null => {
		for (const wrap of view.contentDOM.querySelectorAll<HTMLElement>(CELL_IMAGE)) {
			const range = cellImageRange(view, wrap);
			if (range && range.from < to && range.to > from) return drawnCellImage(wrap) ? cellImageRequest(view, wrap) : 'waiting';
		}
		for (const el of view.contentDOM.querySelectorAll<HTMLElement>(DIAGRAM)) {
			const range = diagramAnchor(view, el);
			if (range && range.from < to && range.to > from) return drawnDiagram(el) ? diagramRequest(view, el) : 'waiting';
		}
		return null;
	};
	if (from < view.viewport.from || from > view.viewport.to) {
		view.dispatch({ effects: EditorView.scrollIntoView(from, { y: 'center' }) });
		await new Promise<void>((resolve) => {
			const timer = setTimeout(resolve, 500);
			view.requestMeasure({ read: () => undefined, write: () => { clearTimeout(timer); resolve(); } });
		});
	}
	const deadline = Date.now() + DRAW_WAIT_MS;
	for (let request = found(); ; request = found()) {
		if (request !== 'waiting') return request;
		if (Date.now() > deadline) return null;
		await new Promise((resolve) => setTimeout(resolve, 100));
	}
}
