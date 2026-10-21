import { Decoration, type DecorationSet, EditorView, keymap, WidgetType } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import { Facet, Prec, RangeSet, StateField, type EditorState, type Range, type Text } from '@codemirror/state';
import type { SyntaxNode } from '@lezer/common';
import { imageContentUrl } from '$lib/artifactImage';
import { FAILED_IMAGE, imageFailureReason, MISSING_IMAGE, NOT_AN_IMAGE, UNAVAILABLE_IMAGE } from '$lib/imageFailure';

export { FAILED_IMAGE, MISSING_IMAGE, NOT_AN_IMAGE, UNAVAILABLE_IMAGE };

/** Where the document lives and how to reach its files; supplied by the editor, never read from global state here. */
export interface ImageContext {
	docPath: string | null;
	projectId: string | null;
	token: string;
}

/** Resolves lazily so a widget built later sees the editor's current document and sign-in. */
export const imageContext = Facet.define<() => ImageContext | null, () => ImageContext | null>({
	combine: (providers) => providers[0] ?? (() => null),
});

export interface ImageSource {
	alt: string;
	/** The workspace file the image points at, or null when it is not one (a web or `data:` URL, an unresolved path). */
	path: string | null;
}

function hasScheme(url: string): boolean {
	return url.startsWith('//') || /^[a-z][a-z\d+.-]*:/i.test(url);
}

/** An absolute path as written, or a relative one joined to the document's directory with `.` and `..` folded. */
export function resolveImagePath(docPath: string | null, url: string): string | null {
	let target = url.trim().replace(/^<(.*)>$/, '$1');
	try {
		target = decodeURIComponent(target);
	} catch {
		return null;
	}
	if (!target || hasScheme(target)) return null;
	if (!target.startsWith('/') && !docPath?.startsWith('/')) return null;
	const parts = target.startsWith('/') ? [] : docPath!.split('/').filter(Boolean).slice(0, -1);
	for (const part of target.split('/')) {
		if (!part || part === '.') continue;
		if (part === '..') parts.pop();
		else parts.push(part);
	}
	return `/${parts.join('/')}`;
}

/** The alt text and target of a lezer `Image` node; the path is null for anything that is not a workspace file. */
export function imageSource(node: SyntaxNode, doc: Text, docPath: string | null): ImageSource {
	const marks = node.getChildren('LinkMark');
	const url = node.getChild('URL');
	const alt = marks.length > 1 ? doc.sliceString(marks[0].to, marks[1].from) : '';
	return { alt, path: url ? resolveImagePath(docPath, doc.sliceString(url.from, url.to)) : null };
}

export function renderableImage(state: EditorState, node: SyntaxNode): boolean {
	const context = state.facet(imageContext)();
	return context !== null && imageSource(node, state.doc, context.docPath).path !== null;
}

/**
 * Draws a workspace image into `wrap`, as the document does standalone or in a table cell. A failed load shows the
 * alt text and why, from the server's answer to the same request: the file is missing (404), refused as outside this
 * scope (400) or not an image (415); any other status, or no answer at all, is a plain failure to load.
 */
export function drawImage(wrap: HTMLElement, alt: string, src: string): HTMLElement {
	const img = document.createElement('img');
	img.alt = alt;
	img.decoding = 'async';
	img.addEventListener('error', async () => {
		const reason = await imageFailureReason(src);
		const note = document.createElement('div');
		note.className = 'cm-md-image-note';
		note.textContent = alt ? `${alt} · ${reason}` : reason;
		wrap.replaceChildren(note);
	});
	img.src = src;
	wrap.append(img);
	return wrap;
}

/**
 * The image, drawn below its line, or in place of the line when the image is all it holds. It carries the
 * range of its markdown (`data-from`, `data-to`) so a click can select it whole, and shows when it is selected.
 */
class ImageWidget extends WidgetType {
	constructor(readonly alt: string, readonly src: string, readonly from: number, readonly to: number, readonly selected: boolean) { super(); }
	eq(other: ImageWidget) {
		return other.src === this.src && other.alt === this.alt && other.from === this.from && other.to === this.to && other.selected === this.selected;
	}
	/** A moved or newly selected image keeps its element, so it does not reload. */
	updateDOM(dom: HTMLElement) {
		if (dom.dataset.src !== this.src || dom.dataset.alt !== this.alt) return false;
		this.mark(dom);
		return true;
	}
	private mark(wrap: HTMLElement) {
		wrap.dataset.from = String(this.from);
		wrap.dataset.to = String(this.to);
		wrap.classList.toggle('cm-md-image-selected', this.selected);
	}
	toDOM() {
		const wrap = document.createElement('div');
		wrap.className = 'cm-md-image';
		wrap.dataset.src = this.src;
		wrap.dataset.alt = this.alt;
		this.mark(wrap);
		return drawImage(wrap, this.alt, this.src);
	}
	ignoreEvent() { return false; }
}

interface Images {
	decorations: DecorationSet;
	/** The markdown of each drawn image, which the caret steps over and an edit removes whole. */
	atomic: DecorationSet;
}

const NONE: Images = { decorations: Decoration.none, atomic: Decoration.none };
const UNIT = Decoration.mark({});

/**
 * An image's markdown never shows. A line holding only the image is replaced by it, so no empty line is left
 * above; with other text on the line the markdown alone is hidden and the image sits below the line.
 */
function buildImages(state: EditorState): Images {
	const decos: Range<Decoration>[] = [];
	const atomic: Range<Decoration>[] = [];
	const { doc, selection } = state;
	const context = state.facet(imageContext)();
	if (!context) return NONE;
	syntaxTree(state).iterate({
		enter: (node) => {
			if (node.name === 'Table' || node.name === 'FencedCode') return false;
			if (node.name !== 'Image') return;
			const { alt, path } = imageSource(node.node, doc, context.docPath);
			if (path === null) return false;
			const line = doc.lineAt(node.from);
			const src = imageContentUrl({ path, projectId: context.projectId, token: context.token });
			const selected = selection.ranges.some((r) => !r.empty && r.from <= node.from && r.to >= node.to);
			const widget = new ImageWidget(alt, src, node.from, node.to, selected);
			if (line.text.trim() === doc.sliceString(node.from, node.to)) {
				decos.push(Decoration.replace({ widget, block: true }).range(line.from, line.to));
				// The line break goes with it, so Backspace on the next line removes the image, not the break.
				atomic.push(UNIT.range(line.from, Math.min(line.to + 1, doc.length)));
			} else {
				decos.push(Decoration.widget({ widget, block: true, side: 1 }).range(line.to));
				decos.push(Decoration.replace({}).range(node.from, node.to));
				atomic.push(UNIT.range(node.from, node.to));
			}
			return false;
		}
	});
	return { decorations: Decoration.set(decos, true), atomic: RangeSet.of(atomic, true) };
}

/** Block decorations must come from a state field; this one follows the tree, the document and the selection (which marks the selected image). */
export const markdownImages = StateField.define<Images>({
	create: buildImages,
	update(images, tr) {
		const selectionMoved = tr.selection !== undefined && images.atomic.size > 0;
		if (tr.docChanged || selectionMoved || syntaxTree(tr.state) !== syntaxTree(tr.startState)) return buildImages(tr.state);
		return images;
	},
	provide: (f) => [
		EditorView.decorations.from(f, (images) => images.decorations),
		EditorView.atomicRanges.of((view) => view.state.field(f).atomic),
	],
});

/** What the canvas needs to show one image of the document larger, and to put the document back as it was. */
export interface OpenImageRequest {
	src: string;
	alt: string;
	/** The image's markdown range in the document. */
	from: number;
	to: number;
	scrollTop: number;
	/** How far below the top of the scrolled text the image sits; it is held there when the document is put back. */
	top: number;
}

/** Supplied by the editor's host; with none, an image is only selected and never opens. */
export const imageOpener = Facet.define<(request: OpenImageRequest) => void, ((request: OpenImageRequest) => void) | null>({
	combine: (openers) => openers[0] ?? null,
});

/** The widget of an image that has drawn; one still loading or failed has nothing to enlarge. */
function loadedImageWidget(target: EventTarget | null): HTMLElement | null {
	const wrap = (target as Element | null)?.closest?.<HTMLElement>('.cm-md-image') ?? null;
	const img = wrap?.querySelector('img');
	return wrap && img && img.complete && img.naturalWidth > 0 ? wrap : null;
}

function requestFromWidget(view: EditorView, wrap: HTMLElement): OpenImageRequest | null {
	const [from, to] = [Number(wrap.dataset.from), Number(wrap.dataset.to)];
	if (!(to > from) || to > view.state.doc.length) return null;
	const top = wrap.getBoundingClientRect().top - view.scrollDOM.getBoundingClientRect().top;
	return { src: wrap.dataset.src ?? '', alt: wrap.dataset.alt ?? '', from, to, scrollTop: view.scrollDOM.scrollTop, top };
}

function openFromWidget(view: EditorView, wrap: HTMLElement): boolean {
	const open = view.state.facet(imageOpener);
	const request = open ? requestFromWidget(view, wrap) : null;
	if (!open || !request) return false;
	open(request);
	return true;
}

/** How long an image scrolled into view may take to draw before opening it is given up. */
const DRAW_WAIT_MS = 4000;

/**
 * What opening the image whose markdown is at `from`..`to` needs, as a click on it would give: for a comment on it,
 * opened from the margin or the comments drawer. An image off screen is scrolled to first and waited for; one that
 * does not draw gives null, and the caller keeps its comments in their cards.
 */
export async function imageRequestAt(root: HTMLElement, { from, to }: { from: number; to: number }): Promise<OpenImageRequest | null> {
	const editor = root.querySelector<HTMLElement>('.cm-editor');
	const view = editor ? EditorView.findFromDOM(editor) : null;
	if (!view || from > view.state.doc.length) return null;
	const widget = () => [...view.contentDOM.querySelectorAll<HTMLElement>('.cm-md-image')]
		.find((wrap) => Number(wrap.dataset.from) < to && Number(wrap.dataset.to) > from) ?? null;
	// Outside the rendered part of the document the image has no widget yet.
	if (from < view.viewport.from || from > view.viewport.to) {
		view.dispatch({ effects: EditorView.scrollIntoView(from, { y: 'center' }) });
		await new Promise<void>((resolve) => {
			const timer = setTimeout(resolve, 500);
			view.requestMeasure({ read: () => undefined, write: () => { clearTimeout(timer); resolve(); } });
		});
	}
	const img = widget()?.querySelector('img');
	if (img && !img.complete) {
		await new Promise<void>((resolve) => {
			const done = () => { clearTimeout(timer); resolve(); };
			const timer = setTimeout(done, DRAW_WAIT_MS);
			img.addEventListener('load', done, { once: true });
			img.addEventListener('error', done, { once: true });
		});
	}
	const wrap = loadedImageWidget(widget());
	return wrap ? requestFromWidget(view, wrap) : null;
}

/**
 * A click on a drawn image opens it larger in the canvas. An image that did not draw, or an editor with no canvas to
 * open it in, keeps the older behavior: the click selects its markdown whole, which the Comment control anchors to.
 */
export const selectImageOnClick = EditorView.domEventHandlers({
	mousedown(event, view) {
		const wrap = (event.target as Element | null)?.closest?.<HTMLElement>('.cm-md-image');
		if (!wrap || event.button !== 0) return false;
		if (view.state.facet(imageOpener) && loadedImageWidget(wrap)) {
			// The click that follows opens it; the caret and selection stay where they were.
			event.preventDefault();
			view.focus();
			return true;
		}
		const [from, to] = [Number(wrap.dataset.from), Number(wrap.dataset.to)];
		if (!(to > from) || to > view.state.doc.length) return false;
		event.preventDefault();
		view.dispatch({ selection: { anchor: from, head: to }, userEvent: 'select.image' });
		view.focus();
		return true;
	},
	click(event, view) {
		const wrap = event.button === 0 ? loadedImageWidget(event.target) : null;
		if (!wrap || !openFromWidget(view, wrap)) return false;
		event.preventDefault();
		return true;
	},
});

/** Enter on an image that is the whole selection opens it, so the keyboard reaches what a click does. */
export const openSelectedImageOnEnter = Prec.high(keymap.of([{
	key: 'Enter',
	run(view) {
		const selected = view.dom.querySelectorAll<HTMLElement>('.cm-md-image-selected');
		const wrap = selected.length === 1 ? loadedImageWidget(selected[0]) : null;
		return wrap ? openFromWidget(view, wrap) : false;
	},
}]));
