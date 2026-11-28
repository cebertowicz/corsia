// What a comment's failed post says, in its box or its toast: the same words on every surface for what the server answered.
import { RequestRefused } from '$lib/api/docs';
import { CAPTURE_FAILURE, SELECTION_LOST } from '$lib/stores/commentSelection';
import { plainErrorText } from '$lib/plainError';
import { POST_FAILED } from '$lib/docViewerOperations';
import { DRAFT_KEPT } from '$lib/stores/draftKept';
import { CommentConnectionError } from '$lib/api/boundedRequest';

/** What a person is told when the diagram changed under their pin; the server's `diagram_changed` refusal carries the same words. */
export const DIAGRAM_CHANGED_MESSAGE = `Place the pin on the current diagram. ${DRAFT_KEPT}`;
/** The server refused a pin because the diagram it was drawn on is not the one shown now. */
export const isDiagramChanged = (error: unknown) => error instanceof RequestRefused && error.kind === 'diagram_changed';

/** What a failed post of a comment anchored in a live document says; an image's region comment there says the same. */
export function liveFailureMessage(error: unknown): string {
	if (error instanceof Error && error.message === SELECTION_LOST) return `The selected text changed or was removed. ${DRAFT_KEPT}`;
	if (error instanceof Error && error.message.startsWith('409 ')) return CAPTURE_FAILURE.changed;
	return knownFailure(error) ?? (error instanceof Error ? error.message : POST_FAILED);
}

/** What a failed post of a comment that is not in a live document says: the same words for what the server answered. */
export function postFailureMessage(error: unknown): string {
	// A 409 here is the server's own bookkeeping (a reused id, a closing document), not words for the reader.
	if (error instanceof Error && error.message.startsWith('409 ')) return POST_FAILED;
	return knownFailure(error) ?? POST_FAILED;
}

/** The words for a failure every composer shares, or null when the error is none of them. */
function knownFailure(error: unknown): string | null {
	if (!(error instanceof Error)) return POST_FAILED;
	// A timed-out post may already be committed, and its reservation is released: asking to try again invites a duplicate.
	if (error.name === 'PostTimeoutError') return `Couldn’t confirm the comment was posted. ${DRAFT_KEPT} Check the comments before you post it again.`;
	// A lost connection and a server error ("500 <body>") carry wire text, not words for the reader.
	if (error instanceof TypeError || error instanceof CommentConnectionError || /^5\d{2}\b/.test(error.message)) return POST_FAILED;
	if (/^\d{3}\b/.test(error.message)) return refusalMessage(error.message);
	return null;
}

/** A 4xx refusal ("<status> <body>" from `ok()`): the server's reason through the shared gate, otherwise a plain failure. */
function refusalMessage(wire: string): string {
	const [, status, body] = /^(\d{3})\s*([\s\S]*)$/.exec(wire)!;
	let detail: unknown;
	try { detail = JSON.parse(body)?.detail; } catch { /* A body that is not JSON has no reason to show. */ }
	if (detail !== undefined) console.error('[api] request rejected', Number(status), detail);
	const reason = plainErrorText(detail, '').replace(/\.+$/, '');
	return reason ? `Couldn’t post the comment: ${reason}. ${DRAFT_KEPT}` : POST_FAILED;
}
