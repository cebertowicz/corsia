import * as Y from 'yjs';
import { encodeUpdate, newClientId, textDigest } from './documentWire';
import type { LiveDocumentSession } from './liveDocumentSession';
import { DRAFT_KEPT } from './draftKept';
import { CaptureFailed, type CaptureCode } from './captureFailed';
import { SERVER_SAVE_MS } from '$lib/api/boundedRequest';

export const SELECTION_LOST = 'The text you selected changed or was removed. Select the text again; your draft will carry over.';
export const SELECTION_RECONNECTED = 'The document reconnected, so this comment lost its place. Select the text again; your draft will carry over.';
export interface CommentSelection {
	session: LiveDocumentSession;
	from: Y.RelativePosition;
	to: Y.RelativePosition;
	quote: string;
}
export interface CapturedComment {
	from: number;
	to: number;
	quote: string;
	clientId: string;
}
export function selectComment(session: LiveDocumentSession, from: number, to: number): CommentSelection {
	return { session, from: Y.createRelativePositionFromTypeIndex(session.text, from, 0),
		to: Y.createRelativePositionFromTypeIndex(session.text, to, -1), quote: session.text.toString().slice(from, to) };
}
/** Where the selection is now, with the text there now: an edit inside it changes its quote, and only a gone or empty range is lost. */
export function selectionRange(selection: CommentSelection): { from: number; to: number; quote: string } | null {
	const { session } = selection;
	const from = Y.createAbsolutePositionFromRelativePosition(selection.from, session.doc)?.index;
	const to = Y.createAbsolutePositionFromRelativePosition(selection.to, session.doc)?.index;
	return from !== undefined && to !== undefined && from < to ? { from, to, quote: session.text.toString().slice(from, to) } : null;
}
/**
 * How long a capture keeps asking the server to track its range, counted from its start, on a document that keeps moving.
 * It bounds re-asks only: an ask already out when it runs out waits for its answer up to ANSWER_ALLOWANCE_MS.
 */
export const RESERVE_BUDGET_MS = 3000;
/**
 * How long one ask may wait for the server's answer, from when it was sent: the comment POST's own allowance for the
 * server (`SERVER_SAVE_MS`). The ask goes over the document's open socket, so it never waits for a browser connection as
 * the POST can. A slow but working server answers within it; one that never answers ends the capture `unanswered`, at
 * most RESERVE_BUDGET_MS + ANSWER_ALLOWANCE_MS (13 s) after the press.
 */
export const ANSWER_ALLOWANCE_MS = SERVER_SAVE_MS;
/** After a refusal, how long a capture waits for the remote update the server already holds before it asks again. */
export const CATCH_UP_MS = 50;
/** Why a capture ended without a place for its comment; each is a reason the comment's box shows. */
export const CAPTURE_FAILURE = {
	changed: `The document changed before your comment could be attached. ${DRAFT_KEPT} Try again.`,
	offline: `You’re offline. ${DRAFT_KEPT} Try again when connected.`,
	unanswered: `Couldn’t attach your comment. ${DRAFT_KEPT} Try again.`,
	settling: `Live updates are still settling. ${DRAFT_KEPT} Try again.`,
} as const;
const { changed: CHANGED, offline: OFFLINE, unanswered: UNANSWERED } = CAPTURE_FAILURE;
/** The server already holds a comment, or another person's capture, under the id asked with (`identity_in_use`). */
export class IdentityInUseError extends CaptureFailed {
	constructor() {
		super('identity', UNANSWERED);
		this.name = 'IdentityInUseError';
	}
}
const failed = (code: CaptureCode, message: string) => new CaptureFailed(code, message);

/**
 * One correlated capture owned by the existing draft. It asks the server to track the selection under the
 * comment's own client id, which succeeds when the server holds, or recently held, the state this browser hashed
 * and the selection still holds the quote sent with it. A hash a
 * remote update interrupts is simply redone. After a refusal it waits for the next applied remote update (or
 * CATCH_UP_MS), so the browser has caught up, and asks again until RESERVE_BUDGET_MS runs out. An ask still out
 * then is waited for until ANSWER_ALLOWANCE_MS after it was sent. Abort, refusal, or an unanswered reservation gives
 * the range back; abort also releases an in-progress hash. `clientId` is the comment draft's own id, the same for
 * every press of Comment on it. A failure is a `CaptureFailed`, whose code says which way it ended.
 */
export function captureComment(selection: CommentSelection, seq: number, signal: AbortSignal, clientId = newClientId()): Promise<CapturedComment> {
	const { session } = selection;
	return new Promise((resolve, reject) => {
		const started = performance.now();
		let done = false, requestId: string | null = null, sent: CapturedComment | null = null;
		let deadline: ReturnType<typeof setTimeout> | undefined, answer: ReturnType<typeof setTimeout> | undefined;
		let stopFrame = () => {}, stopState = () => {}, stopWait = () => {};
		const finish = (result?: CapturedComment, error?: Error) => {
			if (done) return;
			done = true; clearTimeout(deadline); clearTimeout(answer); stopFrame(); stopState(); stopWait(); signal.removeEventListener('abort', abort);
			session.cancelReservationRequest(requestId);
			if (result) { resolve(result); return; }
			if (sent) session.releaseReservation(clientId);
			reject(error ?? failed('changed', CHANGED));
		};
		const abort = () => finish(undefined, new DOMException('Comment selection request cancelled', 'AbortError'));
		if (signal.aborted) { abort(); return; }
		signal.addEventListener('abort', abort, { once: true });
		stopFrame = session.onFrame(frame => {
			// No ask is out yet while hashing: a refusal without an id (an earlier capture's release) is not ours.
			if (requestId === null || frame.request_id !== requestId) return;
			if (frame.type === 'doc_error') {
				if (frame.kind === 'identity_in_use') { finish(undefined, new IdentityInUseError()); return; }
				finish(undefined, failed('refused', (frame.kind === 'refused' || frame.kind === 'store_refused') && frame.message ? frame.message : CHANGED));
				return;
			}
			if (frame.artifact_id !== session.artifactId || frame.store_id !== session.storeId || frame.type !== 'doc_positions' || frame.seq !== seq || !sent) return;
			if (frame.reserved) finish(sent); else again(true);
		});
		stopState = session.subscribe(() => {
			if (session.connection !== 'connected') finish(undefined, failed('offline', OFFLINE));
		});
		if (done) { stopState(); return; }
		// With no ask out, the budget ran out on a document that kept moving. An ask still out is left to its own allowance.
		deadline = setTimeout(() => { if (!sent) finish(); }, RESERVE_BUDGET_MS);
		void ask();
		function again(refused: boolean) {
			sent = null; clearTimeout(answer);
			if (performance.now() - started >= RESERVE_BUDGET_MS) finish(); else if (refused) catchUp(); else void ask();
		}
		function catchUp() {
			const generation = session.textGeneration;
			let stop = () => {}, timer: ReturnType<typeof setTimeout> | undefined;
			const go = () => { clearTimeout(timer); stop(); stopWait = () => {}; if (!done) void ask(); };
			stopWait = () => { clearTimeout(timer); stop(); };
			stop = session.subscribe(() => { if (session.textGeneration !== generation) go(); });
			timer = setTimeout(go, CATCH_UP_MS);
		}
		async function ask() {
			const generation = session.textGeneration, before = session.durability.snapshot(), text = session.text.toString();
			try {
				const digest = await textDigest(text), after = session.durability.snapshot();
				if (done) return;
				if (generation !== session.textGeneration || before.localGeneration !== after.localGeneration
					|| before.ackGeneration !== after.ackGeneration) { again(false); return; }
				if (!session.durability.clean) { finish(undefined, failed('settling', CAPTURE_FAILURE.settling)); return; }
				const range = selectionRange(selection);
				if (!range) { finish(undefined, failed('lost', SELECTION_LOST)); return; }
				const vector = encodeUpdate(Y.encodeStateVector(session.doc));
				sent = { ...range, clientId };
				requestId = session.requestPositions(seq, false, { client_id: clientId, from_off: range.from, to_off: range.to,
					vector, digest, text_length_utf16: text.length, quote: range.quote });
				if (!requestId) { finish(undefined, failed('offline', OFFLINE)); return; }
				answer = setTimeout(() => finish(undefined, failed('unanswered', UNANSWERED)), ANSWER_ALLOWANCE_MS);
			} catch (error) { finish(undefined, failed('hash', error instanceof Error ? error.message : 'Could not check the selection.')); }
		}
	});
}
