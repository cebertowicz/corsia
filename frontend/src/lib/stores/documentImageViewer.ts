import { writable } from 'svelte/store';
import type { OpenImageRequest } from '$lib/cm/markdown-image';
import type { ImageRegion } from '$lib/imageRegions';
import { remapOccurrence } from '$lib/docImageComments';

/** What the viewer needs to show and take comments on parts of an image inside a document (DCOL-FR13.3). */
export interface DocumentImageComments {
	/** The document's artifact: its comments are the image's. */
	artifactId: string;
	projectId: string | null;
	readOnly: boolean;
	/** Post a region comment anchored on the image's markdown, where it is now. */
	post: (from: number, to: number, region: ImageRegion, text: string) => Promise<string>;
	/** What a failed post says. */
	failure?: (error: unknown) => string;
}

/** An image of a document to show larger, with its comments when the document has them. */
export interface DocumentImageRequest extends OpenImageRequest {
	comments?: DocumentImageComments;
	/** Open on this comment's tag; with `move`, start placing its pin again. */
	focus?: { id: string; move?: boolean };
}

/** An image of a document shown larger in the canvas, for the one tab it was opened from. */
export interface OpenDocumentImage extends DocumentImageRequest {
	instanceId: string;
}

/**
 * At most one per thread. Opening it never touches the tab: the document stays mounted under the viewer, so its
 * live session, draft and place are exactly as they were when the viewer closes. In memory only: a reload shows
 * the document.
 */
export const documentImageViewers = writable<Record<string, OpenDocumentImage>>({});

export function openDocumentImage(threadId: string, image: OpenDocumentImage): void {
	documentImageViewers.update((open) => ({ ...open, [threadId]: image }));
}

export function closeDocumentImage(threadId: string): void {
	documentImageViewers.update((open) => {
		if (!(threadId in open)) return open;
		const { [threadId]: _closed, ...rest } = open;
		return rest;
	});
}

/** The document under an open image changed: the image keeps to its markdown, so its comments and Back stay with it. */
export function remapDocumentImage(threadId: string, instanceId: string, mapPos: (pos: number) => number): void {
	documentImageViewers.update((open) => {
		const image = open[threadId];
		if (!image || image.instanceId !== instanceId) return open;
		const moved = remapOccurrence(image, mapPos);
		return moved === image ? open : { ...open, [threadId]: moved };
	});
}
