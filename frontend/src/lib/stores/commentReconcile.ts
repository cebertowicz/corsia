import { fetchComments, postComment, type ServerComment } from '$lib/api/docs';
import { COMMENT_LOOKUP_TIMEOUT_MS } from '$lib/api/boundedRequest';
import { timed } from './commentPostTiming';
import { checkVersion, type DraftVersion, type PostFields } from './commentVersion';

function findById(rows: ServerComment[], id: string): ServerComment | null {
	for (const r of rows) {
		if (r.id === id) return r;
		const reply = (r.replies ?? []).find((x) => x.id === id);
		if (reply) return reply;
	}
	return null;
}

/**
 * The comment stored under `clientId`, looked for within COMMENT_LOOKUP_TIMEOUT_MS; null when there is none or the look
 * failed. With `again` (an earlier press used the id) it must hold this press's version (`checkVersion`).
 */
export async function findPosted(artifactId: string, clientId: string, c: DraftVersion, again: boolean): Promise<ServerComment | null> {
	const result = await timed(clientId, 'rec', () => fetchComments(artifactId, COMMENT_LOOKUP_TIMEOUT_MS)).catch(() => null);
	const found = result && findById(result.comments, clientId);
	return found && again ? checkVersion(found, c) : found;
}

/**
 * Post under `clientId`; a POST that fails looks for the comment it may have stored. Only when `again` (an earlier press
 * of Comment used the id) can the stored comment be another press's, so only then is its version checked.
 */
export async function postReconciled(artifactId: string, clientId: string, c: PostFields, again = false): Promise<ServerComment> {
	let row: ServerComment;
	try {
		row = await postComment(artifactId, { ...c, client_id: clientId });
	} catch (e) {
		const found = await findPosted(artifactId, clientId, c, again);
		if (found) return found;
		throw e;
	}
	return again ? checkVersion(row, c) : row;
}
