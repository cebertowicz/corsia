// What a pressed comment's post sent again without the person (`unsentComments`) does next, by what the server answered.
import { RequestRefused } from '$lib/api/docs';
import { CommentSendRefused } from '$lib/chatCommentAnchors';
import { DocOpenError, ImageFileError } from '$lib/docOpenRefusal';
import { EarlierVersionPosted } from '$lib/stores/commentVersion';
import { notCommentableMessage, TAG_REFUSALS } from '$lib/docViewerOperations';
import { postFailureMessage } from '$lib/commentFailureMessage';
import { retryCommentConnection, retryCommentStatus } from '$lib/commentRetry';

/** A 409's `kind` when the draft's id names another document's or author's comment (backend CLIENT_ID_CLASH_KIND). */
export const CLIENT_ID_CLASH_KIND = 'client_id_clash';
/** A 409's `kind` when the live document holds the draft's id but has not stored it yet: the same post goes through later. */
export const IDENTITY_SETTLING_KIND = 'identity_settling';

/**
 * - `retry`: a transient connection or server failure, or a 409 the server names no reason for: the next trigger
 *   tries again under the same id, and nothing is said meanwhile.
 * - `fresh`: the draft's id is another comment's: it goes once more under a new id.
 * - `earlier`: the comment stored under the id was posted from other text; that one shows, and the draft keeps the rest.
 * - `refused`: the server will not take it as it is (its passage is gone, the document or thread is gone, a tag is not
 *   allowed): it is never sent again without the person.
 */
export type UnsentOutcome = 'retry' | 'fresh' | 'earlier' | 'refused';

const statusOf = (error: unknown): number | null => {
	const status = error instanceof Error ? /^(\d{3})\b/.exec(error.message)?.[1] : undefined;
	return status ? Number(status) : null;
};

/** A short retry reason without the comment or its quoted passage. */
export function unsentRetryCode(error: unknown): string {
	if (error instanceof CommentSendRefused) return error.retryCode;
	const status = statusOf(error);
	return status === null ? 'connection' : String(status);
}

export function unsentOutcome(error: unknown): UnsentOutcome {
	if (error instanceof EarlierVersionPosted) return 'earlier';
	if (error instanceof DocOpenError || error instanceof ImageFileError) return 'refused';
	// A chat's post says itself whether it was refused, in the sentence its box shows.
	if (error instanceof CommentSendRefused) return error.passes ? 'retry' : 'refused';
	const status = statusOf(error);
	if (status === null) return retryCommentConnection(error) ? 'retry' : 'refused';
	if (retryCommentStatus(status)) return 'retry';
	if (status !== 409) return 'refused';
	const kind = error instanceof RequestRefused ? error.kind : null;
	if (kind === CLIENT_ID_CLASH_KIND) return 'fresh';
	return kind === null || kind === IDENTITY_SETTLING_KIND ? 'retry' : 'refused';
}

/**
 * Why a refused or earlier-posted draft did not go, in the words its box and its notice show. A 409 with a reason is the
 * server's own sentence (its passage is gone, the document no longer takes comments), which says the draft is kept.
 */
export function unsentReason(error: unknown): string {
	if (error instanceof EarlierVersionPosted || error instanceof CommentSendRefused) return error.message;
	const sentence = notCommentableMessage(error) ?? (error instanceof RequestRefused && error.kind ? TAG_REFUSALS[error.kind] : undefined);
	if (sentence) return sentence;
	if (statusOf(error) === 409) {
		let detail: unknown;
		try { detail = JSON.parse((error as Error).message.replace(/^\d{3}\s*/, ''))?.detail; } catch { /* no sentence to show */ }
		if (typeof detail === 'string' && detail) return detail;
	}
	return postFailureMessage(error);
}
