import type { DocComment } from '$lib/stores/docComments';

/**
 * Comments on parts of an image inside a document (DCOL-FR13.3, 13.5, 13.6). Each one is anchored on the image's
 * markdown, so the image it is on is the place its anchor sits: these read that place, never a second index.
 */

/** One image of a document, by its markdown range. */
export type ImageOccurrence = { from: number; to: number };

/**
 * Comments on an image (DCOL-FR13.6): its thumbnail heads the list, cards carry their tag's number and colour.
 * `hoveredId` and `onHover` link each card with its mark on the image; the drawer scrolls to the selected card itself.
 */
export type ImageCommentSurface = { src: string; alt?: string; colorOf: (comment: DocComment) => string; onMovePin?: (id: string) => void; onOpen?: () => void; showThumbnail?: boolean; imageBounds?: () => DOMRect | null; hoveredId?: string | null; onHover?: (id: string | null) => void; picks?: number };

/**
 * A document's images with comments on parts of them (DCOL-FR13.6): each image's comments sit together in the drawer
 * under its thumbnail, and their cards carry their tag's number and colour. `src` is null for an image with no file.
 */
export type DocImageComments = { srcOf: (comment: DocComment) => string | null; colorOf: (comment: DocComment) => string; onOpen: (comment: DocComment, focus?: { id: string; move?: boolean }) => void };

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

/**
 * The drawer row a comment is listed under: one per image of the document, so its comments sit together under one
 * thumbnail. A comment whose image markdown is gone still groups by its file. Text comments have none.
 */
export function imageGroupKey(comment: DocComment): string | null {
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
