import { Decoration, type DecorationSet, EditorView, WidgetType } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import { Facet, StateField, type EditorState, type Range, type Text } from '@codemirror/state';
import type { SyntaxNode } from '@lezer/common';
import { imageContentUrl } from '$lib/artifactImage';

/** Where the document lives and how to reach its files; supplied by the editor, never read from global state here. */
export interface ImageContext {
	docPath: string | null;
	projectId: string | null;
	token: string;
}

export const MISSING_IMAGE = 'Image not found';
export const UNAVAILABLE_IMAGE = 'Image not available here';
export const NOT_AN_IMAGE = 'Not an image';
export const FAILED_IMAGE = "Couldn't load this image";

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
 * The image, drawn below its line. A failed load shows the alt text and why, from the
 * server's answer to the same request: the file is missing (404), refused as outside this
 * scope (400) or not an image (415); any other status, or no answer at all, is a plain
 * failure to load.
 */
class ImageWidget extends WidgetType {
	constructor(readonly alt: string, readonly src: string) { super(); }
	eq(other: ImageWidget) { return other.src === this.src && other.alt === this.alt; }
	toDOM() {
		const wrap = document.createElement('div');
		wrap.className = 'cm-md-image';
		const img = document.createElement('img');
		img.alt = this.alt;
		img.decoding = 'async';
		img.addEventListener('error', () => void this.showFailure(wrap));
		img.src = this.src;
		wrap.append(img);
		return wrap;
	}
	private async showFailure(wrap: HTMLElement) {
		let reason = FAILED_IMAGE;
		try {
			const res = await fetch(this.src);
			void res.body?.cancel();
			if (res.status === 404) reason = MISSING_IMAGE;
			else if (res.status === 400) reason = UNAVAILABLE_IMAGE;
			else if (res.status === 415) reason = NOT_AN_IMAGE;
		} catch { /* Offline or aborted: the answer is unknown, so it stays a plain failure. */ }
		const note = document.createElement('div');
		note.className = 'cm-md-image-note';
		note.textContent = this.alt ? `${this.alt} · ${reason}` : reason;
		wrap.replaceChildren(note);
	}
	ignoreEvent() { return false; }
}

function buildImageDecorations(state: EditorState): DecorationSet {
	const decos: Range<Decoration>[] = [];
	const { doc, selection } = state;
	const context = state.facet(imageContext)();
	if (!context) return Decoration.none;
	syntaxTree(state).iterate({
		enter: (node) => {
			if (node.name === 'Table' || node.name === 'FencedCode') return false;
			if (node.name !== 'Image') return;
			const { alt, path } = imageSource(node.node, doc, context.docPath);
			if (path === null) return false;
			const line = doc.lineAt(node.from);
			const src = imageContentUrl({ path, projectId: context.projectId, token: context.token });
			decos.push(Decoration.widget({ widget: new ImageWidget(alt, src), block: true, side: 1 }).range(line.to));
			// The source shows, still editable, only while the caret is on its line.
			if (!selection.ranges.some((r) => r.from <= line.to && r.to >= line.from)) decos.push(Decoration.replace({}).range(node.from, node.to));
			return false;
		}
	});
	return Decoration.set(decos, true);
}

/** Block decorations must come from a state field; this one follows the tree, the document and the caret. */
export const markdownImages = StateField.define<DecorationSet>({
	create: buildImageDecorations,
	update(deco, tr) {
		const caretMoved = tr.selection !== undefined && deco.size > 0;
		if (tr.docChanged || caretMoved || syntaxTree(tr.state) !== syntaxTree(tr.startState)) return buildImageDecorations(tr.state);
		return deco;
	},
	provide: (f) => EditorView.decorations.from(f),
});
