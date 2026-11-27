// Why a live comment's capture ended without a place. The message stays the sentence its box shows; the code is what the
// comment-post timing report carries (`out=failed:cap:<code>`), so a failure's cause reaches the server's log without its
// words. Kept apart from commentSelection so the timing report can tell a capture failure without loading the editor.

/**
 * Each code is one way a capture ends. Every code fits the server's pattern for a short code, `[a-z][a-z_]{0,31}`
 * (`_CODE_CHARS` in backend/api/comment_post_timing.py); the longest, `store_mismatch`, is 14.
 * - `unanswered`: its ask got no answer within the server's allowance.
 * - `changed`: the time budget ran out on a document that kept moving, or the server refused the range after it.
 * - `offline`: the connection was lost, or the ask could not be sent.
 * - `settling`: this browser's own edits were not yet acknowledged.
 * - `lost`: the selected text is gone.
 * - `identity`: the server already holds something under the comment's id.
 * - `refused`: the server refused the ask with an error.
 * - `store_mismatch`: the live store was replaced before the reservation.
 * - `hash`: the browser could not check the selection's text.
 */
export type CaptureCode = 'unanswered' | 'changed' | 'offline' | 'settling' | 'lost' | 'identity' | 'refused' | 'store_mismatch' | 'hash';

export class CaptureFailed extends Error {
	constructor(readonly code: CaptureCode, message: string) {
		super(message);
		this.name = 'CaptureFailed';
	}
}
