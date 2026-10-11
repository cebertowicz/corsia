import { writable } from 'svelte/store';
import type { OpenImageRequest } from '$lib/cm/markdown-image';

/** An image of a document shown larger in the canvas, for the one tab it was opened from. */
export interface OpenDocumentImage extends OpenImageRequest {
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
