import { DocRequestError, patchComment, type ImageRegion } from '$lib/api/docs';
import { docComments, postReconciled, regionFromServer, upsertTop } from './docComments';
import { newClientId } from './documentWire';
import { captureComment, selectComment, SELECTION_LOST } from './commentSelection';
import type { LiveDocumentSession } from './liveDocumentSession';

/** Comment on part of an image artifact: a box, or a point when its width and height are 0. */
export async function addRegionComment(artifactId: string, region: ImageRegion, text: string): Promise<string> {
	const row = await postReconciled(artifactId, newClientId(), { body: text, region, author: 'you', author_kind: 'user' });
	upsertTop(artifactId, row);
	return row.id;
}

/** An image inside a document, by its markdown range in the text on screen. */
export interface DocImageAnchor {
	artifactId: string;
	from: number;
	to: number;
	/** The document's text on screen; its markdown at `from`..`to` is the quote. */
	text: string;
	diagramSource?: string;
	/** A live document tracks the range itself, as it does for a selected quote. */
	session?: LiveDocumentSession;
}

let captures = 0;

/**
 * Comment on part of an image inside a document (DCOL-FR13.3): a text comment anchored on the image's markdown,
 * captured like a selection so it follows the image through edits, plus the region. The server finds the file.
 */
export async function addDocImageComment(image: DocImageAnchor, region: ImageRegion, body: string): Promise<string> {
	const { session } = image;
	if (!session) {
		const row = await postReconciled(image.artifactId, newClientId(), {
			body, region, diagram_source: image.diagramSource, anchor_text: image.text.slice(image.from, image.to), from_off: image.from, to_off: image.to,
			author: 'you', author_kind: 'user',
		});
		upsertTop(image.artifactId, row);
		return row.id;
	}
	const captured = await captureComment(selectComment(session, image.from, image.to), ++captures, new AbortController().signal).catch((error) => {
		// A diagram whose markdown was edited away has lost its range: that is the diagram changing, as the server's refusal says it.
		if (image.diagramSource !== undefined && error instanceof Error && error.message === SELECTION_LOST) throw new DocRequestError(`409 ${SELECTION_LOST}`, 409, 'diagram_changed');
		throw error;
	});
	try {
		const row = await postReconciled(session.artifactId, captured.clientId, {
			body, region, diagram_source: image.diagramSource, anchor_text: captured.quote, from_off: captured.from, to_off: captured.to, live_capture: true,
			author: 'you', author_kind: 'user',
		});
		upsertTop(session.artifactId, row);
		return row.id;
	} catch (error) {
		session.releaseReservation(captured.clientId);
		throw error;
	}
}

/** Move a pin. The server places it on the image as it is now, so it is no longer stale. */
export async function moveRegion(artifactId: string, commentId: string, region: ImageRegion, diagramSource?: string): Promise<void> {
	const row = await patchComment(commentId, { region, diagram_source: diagramSource });
	docComments.update((all) => ({
		...all,
		[artifactId]: (all[artifactId] ?? []).map((c) => (c.id === commentId ? { ...c, ...regionFromServer(row) } : c))
	}));
}
