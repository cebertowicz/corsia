import { plainQuote } from '$lib/plainQuote';
import type { DocComment } from '$lib/stores/docComments';

/**
 * Comments on parts of an image inside a document (DCOL-FR13.3, 13.5, 13.6). Each one is anchored on the image's
 * markdown, so the image it is on is the place its anchor sits: these read that place, never a second index.
 */

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

/** The image's alt text, read from the markdown a region comment is anchored on; empty when it has none. */
export function imageAlt(comment: DocComment): string {
	return /^!\[([^\]]*)\]/.exec(comment.anchor ?? '')?.[1].trim() ?? '';
}

const DIAGRAM_FENCE = /^\s*(?:`{3,}|~{3,})\s*mermaid\s*$/i;

/** Whether a comment's anchor is a mermaid block's opening fence line, where a pin on a diagram is anchored (TDC-FR8). */
export const isDiagramAnchor = (anchor: string | undefined | null): boolean => DIAGRAM_FENCE.test(anchor ?? '');

/** A pin on a rendered diagram: a region comment with no image file, anchored on its block's opening fence line (TDC-FR8). */
export function isDiagramPin(comment: DocComment): boolean {
	return !!comment.region && !comment.imageSrc && isDiagramAnchor(comment.anchor);
}

/** What a pin's card says when what it is on changed after it was placed: the kind of thing it is on, then the one wording. */
export const changedLabel = (anchor: string | undefined | null): string => `${isDiagramAnchor(anchor) ? 'Diagram' : 'Image'} changed since this comment`;

/**
 * The diagram whose opening fence line starts at `from` in the document `text`: its source, and the caption its drawer row
 * carries, naming the nearest heading above it (`Diagram in “Flow”`, or `Diagram` with none). Null when no mermaid block opens there.
 */
export function diagramAt(text: string, from: number): { source: string; caption: string } | null {
	const lines = text.split('\n');
	let offset = 0, heading = '', fence: string | null = null, opening = -1;
	for (let i = 0; i < lines.length; offset += lines[i].length + 1, i++) {
		if (offset > from) break;
		const marker = /^\s{0,3}(`{3,}|~{3,})/.exec(lines[i])?.[1];
		if (offset === from) { if (!fence && DIAGRAM_FENCE.test(lines[i]) && marker) { opening = i; break; } return null; }
		if (marker && (!fence || (marker[0] === fence[0] && marker.length >= fence.length && /^\s{0,3}[`~]+\s*$/.test(lines[i])))) { fence = fence ? null : marker; continue; }
		if (!fence) heading = /^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$/.exec(lines[i])?.[1] ?? heading;
	}
	if (opening < 0) return null;
	const marker = /(`{3,}|~{3,})/.exec(lines[opening])![1];
	const close = lines.findIndex((line, i) => i > opening && new RegExp(`^\\s{0,3}${marker[0]}{${marker.length},}\\s*$`).test(line));
	const source = lines.slice(opening + 1, close < 0 ? lines.length : close).join('\n');
	const name = plainQuote(heading);
	return { source, caption: name ? `Diagram in \u201c${name}\u201d` : 'Diagram' };
}

/**
 * The drawer row a comment is listed under: one per image of the document, so its comments sit together under one
 * thumbnail, and one per diagram, under its name. A comment whose image markdown is gone still groups by its file. Text comments have none.
 */
export function imageGroupKey(comment: DocComment): string | null {
	if (isDiagramPin(comment)) {
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
