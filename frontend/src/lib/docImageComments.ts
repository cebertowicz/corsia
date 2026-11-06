import { EditorState } from '@codemirror/state';
import { markdown } from '@codemirror/lang-markdown';
import { ensureSyntaxTree, syntaxTree } from '@codemirror/language';
import { GFM } from '@lezer/markdown';
import { isMermaidFence, mermaidSource } from '$lib/cm/widget-blocks';
import { plainQuote } from '$lib/plainQuote';
import type { DocComment } from '$lib/stores/docComments';

/**
 * Comments on parts of an image inside a document (DCOL-FR13.3, 13.5, 13.6). Each one is anchored on the image's
 * markdown, so the image it is on is the place its anchor sits: these read that place, never a second index.
 */

/** Image comments (DCOL-FR13.6): the thumbnail heads the list; tags and cards share number, colour, hover and selected-scroll state. */
export type ImageCommentSurface = { src: string; alt?: string; diagram?: boolean; colorOf: (comment: DocComment) => string; onMovePin?: (id: string) => void; onOpen?: () => void; showThumbnail?: boolean; imageBounds?: () => DOMRect | null; hoveredId?: string | null; onHover?: (id: string | null) => void; picks?: number; candidateOrder?: string[] | null };
/** A document's image comments (DCOL-FR13.6): grouped under each thumbnail, with cards carrying tag number and colour; `src` is null when the image has no file. */
export type DocImageComments = { srcOf: (comment: DocComment) => string | null; diagramOf?: (comment: DocComment) => { source: string; caption: string } | null; isDiagram?: (comment: DocComment) => boolean; colorOf: (comment: DocComment) => string; onOpen: (comment: DocComment, focus?: { id: string; move?: boolean }) => void };

/** One image of a document, by its markdown range. */
export type ImageOccurrence = { from: number; to: number };

/** A region comment with a place in the text: the image markdown it is anchored on. */
function placed(comment: DocComment): ImageOccurrence | null {
	return comment.region && !comment.orphaned && comment.from != null && comment.to != null ? { from: comment.from, to: comment.to } : null;
}

/** The region comments on this image of the document; another place showing the same file has its own. */
export function commentsAtImage(comments: DocComment[], image: ImageOccurrence): DocComment[] {
	return comments.filter((comment) => {
		const at = placed(comment);
		return at !== null && at.from < image.to && at.to > image.from;
	});
}

/**
 * The image every one of `ids` is a region comment on, when they share one; a margin marker over such a set opens
 * the image instead of a card. Any text comment among them keeps the cards.
 */
export function sharedImage(comments: DocComment[], ids: string[]): ImageOccurrence | null {
	let shared: ImageOccurrence | null = null;
	for (const id of ids) {
		const comment = comments.find((item) => item.id === id);
		const at = comment ? placed(comment) : null;
		if (!at || (shared && (shared.from !== at.from || shared.to !== at.to))) return null;
		shared = at;
	}
	return shared;
}

/** A comment still on its image in the document; one whose image markdown is gone has nowhere to open. */
export const onItsImage = (comment: DocComment) => !comment.orphaned && comment.from != null;

/** The image's alt text, read from the markdown a region comment is anchored on; empty when it has none. */
export function imageAlt(comment: DocComment): string {
	return /^!\[([^\]]*)\]/.exec(comment.anchor ?? '')?.[1].trim() ?? '';
}

/** In a document, a region comment without an image file is a diagram pin, even after its fence is removed. */
export function isDiagramPin(comment: DocComment): boolean {
	return !!comment.region && !comment.imageSrc;
}

/** The changed-state wording for a diagram or image pin. */
export const changedLabel = (diagram: boolean): string => `${diagram ? 'Diagram' : 'Image'} changed since this comment`;

type Diagram = { source: string; caption: string };
let parsedText: string | undefined;
let diagrams = new Map<number, Diagram>();

/** The diagram at a line anchor and the nearest preceding heading, using the editor's Markdown tree. */
export function diagramAtAnchor(text: string, from: number): Diagram | null {
	if (text !== parsedText) {
		const state = EditorState.create({ doc: text, extensions: [markdown({ extensions: GFM })] });
		const found = new Map<number, Diagram>();
		let heading = '';
		const tree = ensureSyntaxTree(state, state.doc.length, Infinity) ?? syntaxTree(state);
		tree.iterate({ enter(node) {
			if (/^ATXHeading[1-6]$/.test(node.name)) {
				heading = state.doc.sliceString(node.from, node.to).replace(/^#{1,6}\s+|\s+#+\s*$/g, '');
			}
			if (node.name === 'FencedCode' && isMermaidFence(node, state.doc)) {
				const name = plainQuote(heading);
				found.set(state.doc.lineAt(node.from).from, { source: mermaidSource(node, state.doc), caption: name ? `Diagram in \u201c${name}\u201d` : 'Diagram' });
				return false;
			}
		} });
		parsedText = text;
		diagrams = found;
	}
	return diagrams.get(from) ?? null;
}

/**
 * The drawer row a comment is listed under: one per image of the document, so its comments sit together under one
 * thumbnail, and one per diagram, under its name. A comment whose image markdown is gone still groups by its file. Text comments have none.
 */
export function imageGroupKey(comment: DocComment, diagram = false): string | null {
	if (diagram) {
		const at = placed(comment);
		return `diagram\u0000${at ? at.from : 'detached'}`;
	}
	if (!comment.region || !comment.imageSrc) return null;
	const at = placed(comment);
	return `${comment.imageSrc}\u0000${at ? at.from : 'detached'}`;
}

/**
 * The drawer's rows: comments with the same group key sit together at the first one's place, in their number order,
 * and every other comment keeps its own row and place.
 */
export function groupRows<T extends { regionNumber?: number | null }>(comments: T[], key: (comment: T) => string | null): { key: string | null; comments: T[] }[] {
	const rows: { key: string | null; comments: T[] }[] = [];
	const groups = new Map<string, T[]>();
	for (const comment of comments) {
		const at = key(comment);
		const group = at === null ? undefined : groups.get(at);
		if (group) { group.push(comment); continue; }
		const row = { key: at, comments: [comment] };
		if (at !== null) groups.set(at, row.comments);
		rows.push(row);
	}
	for (const group of groups.values()) group.sort((a, b) => (a.regionNumber ?? Infinity) - (b.regionNumber ?? Infinity));
	return rows;
}

/** An open image follows its markdown through edits, as its comments do. */
export function remapOccurrence<T extends ImageOccurrence>(image: T, mapPos: (pos: number) => number): T {
	const from = mapPos(image.from), to = mapPos(image.to);
	return from === image.from && to === image.to ? image : { ...image, from, to };
}
