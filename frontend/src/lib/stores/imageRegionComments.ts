import { patchComment, type ImageRegion } from '$lib/api/docs';
import { docComments, postReconciled, regionFromServer, upsertTop } from './docComments';
import { newClientId } from './documentWire';

/** Comment on part of an image artifact: a box, or a point when its width and height are 0. */
export async function addRegionComment(artifactId: string, region: ImageRegion, text: string): Promise<string> {
	const row = await postReconciled(artifactId, newClientId(), { body: text, region, author: 'you', author_kind: 'user' });
	upsertTop(artifactId, row);
	return row.id;
}

/** Move a pin. The server places it on the image as it is now, so it is no longer stale. */
export async function moveRegion(artifactId: string, commentId: string, region: ImageRegion): Promise<void> {
	const row = await patchComment(commentId, { region });
	docComments.update((all) => ({
		...all,
		[artifactId]: (all[artifactId] ?? []).map((c) => (c.id === commentId ? { ...c, ...regionFromServer(row) } : c))
	}));
}
